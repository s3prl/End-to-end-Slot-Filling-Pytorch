import torch
from torch.nn.utils.rnn import pad_sequence
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.distributed import is_initialized, get_rank, get_world_size

from src.asr import ASR
from src.optim import Optimizer
from src.solver import BaseSolver
from src.data import load_dataset
from src.util import human_format, cal_er, feat_to_fig


class Solver(BaseSolver):
    ''' Solver for training'''

    def __init__(self, config, paras, mode):
        super().__init__(config, paras, mode)
        # Logger settings
        self.best_wer = {'att': 3.0, 'ctc': 3.0}
        # Curriculum learning affects data loader
        self.curriculum = self.config['hparas']['curriculum']

    def fetch_data(self, data):
        ''' Move data to device and compute text seq. length'''
        _, feat, feat_len, txt = data

        if self.paras.upstream is not None:
            feat, feat_len = self.upstream_extractor(wav=feat, wav_len=feat_len)

        feat = feat.to(self.device)
        feat_len = feat_len.to(self.device)
        txt = txt.to(self.device)
        txt_len = torch.sum(txt != 0, dim=-1)

        return feat, feat_len, txt, txt_len

    def load_data(self):
        ''' Load data for training/validation, store tokenizer and input/output shape'''
        self.tr_set, self.dv_set, self.feat_dim, self.vocab_size, self.tokenizer, msg = \
            load_dataset(self.paras.njobs, self.paras.gpu, self.paras.pin_memory,
                         self.curriculum > 0, **self.config['data'],
                         wav_only=self.paras.upstream is not None)
        if self.paras.upstream is not None:
            self.set_upstream()
        self.verbose(msg)

    def set_upstream(self):
        '''Setup pretrained Upstream model'''
        if is_initialized() and get_rank() > 0:
            # While rank 0 is downloading, all other processes wait and set
            # their upstream_refresh to False to prevent double downloading
            self.paras.upstream_refresh = False
            torch.distributed.barrier()

        self.upstream = torch.hub.load(
            's3prl/s3prl',
            self.paras.upstream,
            feature_selection = self.paras.upstream_feature_selection,
            refresh = self.paras.upstream_refresh,
            ckpt = self.paras.upstream_ckpt,
            force_reload = self.paras.upstream_refresh,
        ).to(device=self.device)

        if is_initialized() and get_rank() == 0:
            # After rank 0 downloaded the latest checkpoints, notify
            # others to use this newly downloaded checkpoints
            torch.distributed.barrier()

        if is_initialized():
            self.upstream = DDP(self.upstream, device_ids=[self.paras.local_rank], find_unused_parameters=True)
            setattr(self.upstream, 'get_output_dim', self.upstream.module.get_output_dim)

        if self.paras.upstream_trainable:
            self.upstream.train()
        else:
            self.upstream.eval()

        self.feat_dim = self.upstream.get_output_dim()

    def upstream_extractor(self, wav, wav_len):
        def extract(wav, wav_len):
            feat = self.upstream([w[:l].view(-1).to(self.device) for w, l in zip(wav, wav_len)])
            feat_len = torch.LongTensor([len(f) for f in feat])
            feat = pad_sequence(feat, batch_first=True)
            return feat, feat_len

        if self.upstream.training:
            feat, feat_len = extract(wav, wav_len)
        else:
            with torch.no_grad():
                feat, feat_len = extract(wav, wav_len)

        return feat, feat_len

    def set_model(self):
        ''' Setup ASR model and optimizer '''
        # Model
        init_adadelta = self.config['hparas']['optimizer'] == 'Adadelta'
        self.model = ASR(self.feat_dim, self.vocab_size, init_adadelta, **
                         self.config['model']).to(self.device)

        if is_initialized():
            self.model = DDP(self.model, device_ids=[self.paras.local_rank], find_unused_parameters=True)
            setattr(self.model, 'create_msg', self.model.module.create_msg)
            setattr(self.model, 'ctc_weight', self.model.module.ctc_weight)

        self.verbose(self.model.create_msg())
        model_paras = [{'params': self.model.parameters()}]

        # Losses
        self.seq_loss = torch.nn.CrossEntropyLoss(ignore_index=0)
        # Note: zero_infinity=False is unstable?
        self.ctc_loss = torch.nn.CTCLoss(blank=0, zero_infinity=False)

        # Plug-ins
        self.emb_fuse = False
        self.emb_reg = ('emb' in self.config) and (
            self.config['emb']['enable'])
        if self.emb_reg:
            from src.plugin import EmbeddingRegularizer
            self.emb_decoder = EmbeddingRegularizer(
                self.tokenizer, self.model.dec_dim, **self.config['emb']).to(self.device)
            model_paras.append({'params': self.emb_decoder.parameters()})
            self.emb_fuse = self.emb_decoder.apply_fuse
            if self.emb_fuse:
                self.seq_loss = torch.nn.NLLLoss(ignore_index=0)
            self.verbose(self.emb_decoder.create_msg())

        # Optimizer
        self.optimizer = Optimizer(model_paras, **self.config['hparas'])
        self.verbose(self.optimizer.create_msg())

        # Enable AMP if needed
        self.enable_apex()

        # Automatically load pre-trained model if self.paras.load is given
        self.load_ckpt()

        # ToDo: other training methods

    def exec(self):
        ''' Training End-to-end ASR system '''
        self.verbose('Total training steps {}.'.format(
            human_format(self.max_step)))
        ctc_loss, att_loss, emb_loss = None, None, None
        n_epochs = 0
        self.timer.set()

        while self.step < self.max_step:
            # Renew dataloader to enable random sampling
            if self.curriculum > 0 and n_epochs == self.curriculum:
                self.verbose(
                    'Curriculum learning ends after {} epochs, starting random sampling.'.format(n_epochs))
                self.tr_set, _, _, _, _, _ = \
                    load_dataset(self.paras.njobs, self.paras.gpu, self.paras.pin_memory,
                                 False, **self.config['data'])

            if is_initialized():
                self.tr_set.sampler.set_epoch(n_epochs)

            for data in self.tr_set:
                # Pre-step : update tf_rate/lr_rate and do zero_grad
                tf_rate = self.optimizer.pre_step(self.step)
                total_loss = 0

                # Fetch data
                feat, feat_len, txt, txt_len = self.fetch_data(data)
                self.timer.cnt('rd')

                # Forward model
                # Note: txt should NOT start w/ <sos>
                ctc_output, encode_len, att_output, att_align, dec_state = \
                    self.model(feat, feat_len, max(txt_len).item(), tf_rate=tf_rate,
                               teacher=txt, get_dec_state=self.emb_reg)

                # Plugins
                if self.emb_reg:
                    emb_loss, fuse_output = self.emb_decoder(
                        dec_state, att_output, label=txt)
                    total_loss += self.emb_decoder.weight*emb_loss

                # Compute all objectives
                if ctc_output is not None:
                    if self.paras.cudnn_ctc:
                        ctc_loss = self.ctc_loss(ctc_output.transpose(0, 1),
                                                 txt.to_sparse().values().to(device='cpu', dtype=torch.int32),
                                                 [ctc_output.shape[1]] *
                                                 len(ctc_output),
                                                 txt_len.cpu().tolist())
                    else:
                        ctc_loss = self.ctc_loss(ctc_output.transpose(
                            0, 1), txt, encode_len, txt_len)
                    total_loss += ctc_loss*self.model.ctc_weight

                if att_output is not None:
                    b, t, _ = att_output.shape
                    att_output = fuse_output if self.emb_fuse else att_output
                    att_loss = self.seq_loss(
                        att_output.view(b*t, -1), txt.view(-1))
                    total_loss += att_loss*(1-self.model.ctc_weight)

                self.timer.cnt('fw')

                # Backprop
                grad_norm = self.backward(total_loss)
                self.step += 1

                # Logger
                if (self.step == 1) or (self.step % self.PROGRESS_STEP == 0):
                    self.progress('Tr stat | Loss - {:.2f} | Grad. Norm - {:.2f} | {}'
                                  .format(total_loss.cpu().item(), grad_norm, self.timer.show()))
                    self.write_log(
                        'loss', {'tr_ctc': ctc_loss, 'tr_att': att_loss})
                    self.write_log('emb_loss', {'tr': emb_loss})
                    self.write_log('wer', {'tr_att': cal_er(self.tokenizer, att_output, txt),
                                           'tr_ctc': cal_er(self.tokenizer, ctc_output, txt, ctc=True)})
                    if self.emb_fuse:
                        if self.emb_decoder.fuse_learnable:
                            self.write_log('fuse_lambda', {
                                           'emb': self.emb_decoder.get_weight()})
                        self.write_log(
                            'fuse_temp', {'temp': self.emb_decoder.get_temp()})

                # Validation
                if (self.step == 1) or (self.step % self.valid_step == 0):
                    self.validate()

                # End of step
                # https://github.com/pytorch/pytorch/issues/13246#issuecomment-529185354
                torch.cuda.empty_cache()
                self.timer.set()
                if self.step > self.max_step:
                    break
            n_epochs += 1

        if hasattr(self, 'log'):
            self.log.close()

    def validate(self):
        if is_initialized() and get_rank() > 0:
            return

        # Eval mode
        self.model.eval()
        if self.emb_decoder is not None:
            self.emb_decoder.eval()
        if hasattr(self, 'upstream'):
            upstream_training = self.upstream.training
            self.upstream.eval()
        dev_wer = {'att': [], 'ctc': []}

        for i, data in enumerate(self.dv_set):
            self.progress('Valid step - {}/{}'.format(i+1, len(self.dv_set)))
            # Fetch data
            feat, feat_len, txt, txt_len = self.fetch_data(data)

            # Forward model
            with torch.no_grad():
                ctc_output, encode_len, att_output, att_align, dec_state = \
                    self.model(feat, feat_len, int(max(txt_len)*self.DEV_STEP_RATIO),
                               emb_decoder=self.emb_decoder)

            dev_wer['att'].append(cal_er(self.tokenizer, att_output, txt))
            dev_wer['ctc'].append(cal_er(self.tokenizer, ctc_output, txt, ctc=True))

            # Show some example on tensorboard
            if i == len(self.dv_set)//2:
                for i in range(min(len(txt), self.DEV_N_EXAMPLE)):
                    if self.step == 1:
                        self.write_log('true_text{}'.format(
                            i), self.tokenizer.decode(txt[i].tolist()))
                    if att_output is not None:
                        self.write_log('att_align{}'.format(i), feat_to_fig(
                            att_align[i, 0, :, :].cpu().detach()))
                        self.write_log('att_text{}'.format(i), self.tokenizer.decode(
                            att_output[i].argmax(dim=-1).tolist()))
                    if ctc_output is not None:
                        self.write_log('ctc_text{}'.format(i), self.tokenizer.decode(ctc_output[i].argmax(dim=-1).tolist(),
                                                                                     ignore_repeat=True))

        # Ckpt if performance improves
        for task in ['att', 'ctc']:
            dev_wer[task] = sum(dev_wer[task])/len(dev_wer[task])
            if dev_wer[task] < self.best_wer[task]:
                self.best_wer[task] = dev_wer[task]
                self.save_checkpoint('best_{}.pth'.format(task), 'wer', dev_wer[task])
            self.write_log('wer', {'dv_'+task: dev_wer[task]})
        self.save_checkpoint('latest.pth', 'wer', dev_wer['att'], show_msg=False)

        # Resume training
        self.model.train()
        if self.emb_decoder is not None:
            self.emb_decoder.train()
        if hasattr(self, 'upstream') and upstream_training:
            self.upstream.train()

