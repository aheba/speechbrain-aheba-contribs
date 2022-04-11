#!/usr/bin/env/python3
"""Recipe for training a wav2vec-based ctc ASR system with librispeech.
The system employs wav2vec as its encoder. Decoding is performed with
ctc greedy decoder.
To run this recipe, do the following:
> python train_with_wav2vec.py hparams/train_with_wav2vec.yaml
The neural network is trained on CTC likelihood target and character units
are used as basic recognition tokens. Training is performed on the full
LibriSpeech dataset (960 h).

Authors
 * Sung-Lin Yeh 2021
 * Titouan Parcollet 2021
 * Ju-Chieh Chou 2020
 * Mirco Ravanelli 2020
 * Abdel Heba 2020
 * Peter Plantinga 2020
 * Samuele Cornell 2020
"""

import os
import io
import sys
import torch
import logging
import speechbrain as sb
import torchaudio
from speechbrain.utils.distributed import run_on_main
from hyperpyyaml import load_hyperpyyaml
from pathlib import Path
import webdataset as wds
from speechbrain.dataio.batch import PaddedBatch
import glob

logger = logging.getLogger(__name__)


# Define training procedure
class ASR(sb.Brain):
    def compute_forward(self, batch, stage):
        """Forward computations from the waveform batches to the output probabilities."""
        batch = batch.to(self.device)
        wavs, wav_lens = batch.sig
        wavs, wav_lens = wavs.to(self.device), wav_lens.to(self.device)
        # Add augmentation if specified
        if stage == sb.Stage.TRAIN:
            if hasattr(self.modules, "env_corrupt"):
                wavs_noise = self.modules.env_corrupt(wavs, wav_lens)
                wavs = torch.cat([wavs, wavs_noise], dim=0)
                wav_lens = torch.cat([wav_lens, wav_lens])

            if hasattr(self.hparams, "augmentation"):
                wavs = self.hparams.augmentation(wavs, wav_lens)

        # Forward pass
        feats = self.modules.wav2vec2(wavs)
        x = self.modules.enc(feats)

        # Compute outputs
        p_tokens = None
        logits = self.modules.ctc_lin(x)
        p_ctc = self.hparams.log_softmax(logits)
        if stage != sb.Stage.TRAIN:
            p_tokens = sb.decoders.ctc_greedy_decode(
                p_ctc, wav_lens, blank_id=self.hparams.blank_index
            )
        return p_ctc, wav_lens, p_tokens

    def compute_objectives(self, predictions, batch, stage):
        """Computes the loss (CTC+NLL) given predictions and targets."""
        p_ctc, wav_lens, predicted_tokens = predictions
        ids = batch.id
        tokens, tokens_lens = batch.tokens

        if hasattr(self.modules, "env_corrupt") and stage == sb.Stage.TRAIN:
            tokens = torch.cat([tokens, tokens], dim=0)
            tokens_lens = torch.cat([tokens_lens, tokens_lens], dim=0)

        loss_ctc = self.hparams.ctc_cost(p_ctc, tokens, wav_lens, tokens_lens)
        loss = loss_ctc
        if stage != sb.Stage.TRAIN:
            # Decode token terms to words
            predicted_words = [
                "".join(self.tokenizer.decode_ndim(utt_seq)).split(" ")
                for utt_seq in predicted_tokens
            ]
            target_words = ["".join(self.tokenizer.decode_ndim(utt_tokens[:int(utt_len*utt_tokens.shape[0])])).split(" ") for utt_tokens, utt_len in zip(tokens, tokens_lens)]
            self.wer_metric.append(ids, predicted_words, target_words)
            self.cer_metric.append(ids, predicted_words, target_words)

        return loss

    def fit_batch(self, batch):
        """Train the parameters given a single batch in input"""
        predictions = self.compute_forward(batch, sb.Stage.TRAIN)
        loss = self.compute_objectives(predictions, batch, sb.Stage.TRAIN)
        loss.backward()
        if self.check_gradients(loss):
            self.wav2vec_optimizer.step()
            self.model_optimizer.step()

        self.wav2vec_optimizer.zero_grad()
        self.model_optimizer.zero_grad()

        return loss.detach()

    def evaluate_batch(self, batch, stage):
        """Computations needed for validation/test batches"""
        predictions = self.compute_forward(batch, stage=stage)
        with torch.no_grad():
            loss = self.compute_objectives(predictions, batch, stage=stage)
        return loss.detach()

    def on_stage_start(self, stage, epoch):
        """Gets called at the beginning of each epoch"""
        if stage != sb.Stage.TRAIN:
            self.cer_metric = self.hparams.cer_computer()
            self.wer_metric = self.hparams.error_rate_computer()

    def on_stage_end(self, stage, stage_loss, epoch):
        """Gets called at the end of an epoch."""
        # Compute/store important stats
        stage_stats = {"loss": stage_loss}
        if stage == sb.Stage.TRAIN:
            self.train_stats = stage_stats
        else:
            stage_stats["CER"] = self.cer_metric.summarize("error_rate")
            stage_stats["WER"] = self.wer_metric.summarize("error_rate")

        # Perform end-of-iteration things, like annealing, logging, etc.
        if stage == sb.Stage.VALID:
            old_lr_model, new_lr_model = self.hparams.lr_annealing_model(
                stage_stats["loss"]
            )
            old_lr_wav2vec, new_lr_wav2vec = self.hparams.lr_annealing_wav2vec(
                stage_stats["loss"]
            )
            sb.nnet.schedulers.update_learning_rate(
                self.model_optimizer, new_lr_model
            )
            sb.nnet.schedulers.update_learning_rate(
                self.wav2vec_optimizer, new_lr_wav2vec
            )
            self.hparams.train_logger.log_stats(
                stats_meta={
                    "epoch": epoch,
                    "lr_model": old_lr_model,
                    "lr_wav2vec": old_lr_wav2vec,
                },
                train_stats=self.train_stats,
                valid_stats=stage_stats,
            )
            self.checkpointer.save_and_keep_only(
                meta={"WER": stage_stats["WER"]}, min_keys=["WER"],
            )
        elif stage == sb.Stage.TEST:
            self.hparams.train_logger.log_stats(
                stats_meta={"Epoch loaded": self.hparams.epoch_counter.current},
                test_stats=stage_stats,
            )
            with open(self.hparams.wer_file, "w") as w:
                self.wer_metric.write_stats(w)

    def init_optimizers(self):
        "Initializes the wav2vec2 optimizer and model optimizer"
        self.wav2vec_optimizer = self.hparams.wav2vec_opt_class(
            self.modules.wav2vec2.parameters()
        )
        self.model_optimizer = self.hparams.model_opt_class(
            self.hparams.model.parameters()
        )

        if self.checkpointer is not None:
            self.checkpointer.add_recoverable(
                "wav2vec_opt", self.wav2vec_optimizer
            )
            self.checkpointer.add_recoverable("modelopt", self.model_optimizer)


def dataio_prepare(hparams):
    """This function prepares the datasets to be used in the brain class.
    It also defines the data processing pipeline through user-defined functions."""

    train_data = wds.WebDataset(glob.glob(hparams["train_shards"]), cache_dir=hparams["shard_cache_dir"]).shuffle(hparams["shuffle_shards"])
    valid_data = wds.WebDataset(glob.glob(hparams["valid_shards"]), cache_dir=hparams["shard_cache_dir"]).shuffle(hparams["shuffle_shards"])
    test_data = wds.WebDataset(glob.glob(hparams["test_shards"]), cache_dir=hparams["shard_cache_dir"]).shuffle(hparams["shuffle_shards"])
    label_encoder = sb.dataio.encoder.CategoricalEncoder()
    lab_enc_file = os.path.join(hparams["save_folder"], "label_encoder.txt")
    special_labels = {
        "blank_label": hparams["blank_index"],
    }
    datasets = [ train_data + valid_data + test_data]
    label_encoder.load_or_create(
        path=lab_enc_file,
        special_labels=special_labels,
    )
    #        from_didatasets=datasets,
    #    output_key="text",
    #        sequence_input=True,

    def shard_extractor(input_dict):
        f_IO = io.BytesIO(input_dict["wav"])
        sig, fs = torchaudio.load(f_IO)
        sig = sig.transpose(0, 1).squeeze(1)
        char_list = list(input_dict["text"].decode("utf-8"))
        tokens_list = label_encoder.encode_sequence(char_list)
        tokens = torch.LongTensor(tokens_list)
        return {"id": input_dict["__key__"], "sig" : sig, "tokens": tokens}

    train_data = train_data.map(shard_extractor).batched(hparams["batch_size"], collation_fn=PaddedBatch, partial=False)
    valid_data = valid_data.map(shard_extractor).batched(hparams["batch_size"], collation_fn=PaddedBatch, partial=False)
    test_data = test_data.map(shard_extractor).batched(hparams["test_batch_size"], collation_fn=PaddedBatch, partial=False)


    return train_data, valid_data, test_data, label_encoder


if __name__ == "__main__":

    # CLI:
    hparams_file, run_opts, overrides = sb.parse_arguments(sys.argv[1:])

    # If distributed_launch=True then
    # create ddp_group with the right communication protocol
    sb.utils.distributed.ddp_init_group(run_opts)

    with open(hparams_file) as fin:
        hparams = load_hyperpyyaml(fin, overrides)

    # Create experiment directory
    sb.create_experiment_directory(
        experiment_directory=hparams["output_folder"],
        hyperparams_to_save=hparams_file,
        overrides=overrides,
    )

    # Dataset prep (parsing Librispeech)
    from gigaspeech_prepare import prepare_gigaspeech  # noqa

    # multi-gpu (ddp) save data preparation
    run_on_main(
        prepare_gigaspeech,
        kwargs={
            "data_folder": hparams["data_folder"],
            "save_folder": hparams["output_folder"],
            "train_subset": hparams["train_subset"],
            "skip_prep": hparams["skip_prep"],
        },
    )
    # here we create the datasets objects as well as tokenization and encoding
    train_data, valid_data, test_data, label_encoder = dataio_prepare(
        hparams
    )

    # add collate_fn to dataloader options
    #hparams["train_dataloader_opts"]["collate_fn"] = PaddedBatch
    #hparams["valid_dataloader_opts"]["collate_fn"] = PaddedBatch
    #hparams["test_dataloader_opts"]["collate_fn"] = PaddedBatch

    #hparams["train_dataloader_opts"]["looped_nominal_epoch"] = (
    #    hparams["num_train_samples"] // hparams["train_dataloader_opts"]["batch_size"]
    #)
    #hparams["valid_dataloader_opts"]["looped_nominal_epoch"] = (
    #    hparams["num_valid_samples"] // hparams["valid_dataloader_opts"]["batch_size"]
    #)
    #hparams["test_dataloader_opts"]["looped_nominal_epoch"] = (
    #    hparams["num_test_samples"] // hparams["test_dataloader_opts"]["batch_size"]
    #)

    # Trainer initialization
    asr_brain = ASR(
        modules=hparams["modules"],
        hparams=hparams,
        run_opts=run_opts,
        checkpointer=hparams["checkpointer"],
    )

    # We dynamicaly add the tokenizer to our brain class.
    # NB: This tokenizer corresponds to the one used for the LM!!
    asr_brain.tokenizer = label_encoder

    # Training
    asr_brain.fit(
        asr_brain.hparams.epoch_counter,
        train_data,
        valid_data,
        train_loader_kwargs=hparams["train_dataloader_opts"],
        valid_loader_kwargs=hparams["valid_dataloader_opts"],
    )

    # Testing
    asr_brain.hparams.wer_file = os.path.join(
            hparams["output_folder"], "wer_test.txt"
        )
    asr_brain.evaluate(
            test_data, test_loader_kwargs=hparams["test_dataloader_opts"]
    )