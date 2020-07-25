# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
from typing import Dict, Optional

import torch
from omegaconf import DictConfig
from pytorch_lightning import Trainer

from nemo.collections.common.losses import CrossEntropyLoss
from nemo.collections.common.tokenizers.tokenizer_utils import get_tokenizer
from nemo.collections.nlp.data.text_classification import TextClassificationDataDesc, TextClassificationDataset
from nemo.collections.nlp.metrics.classification_report import ClassificationReport
from nemo.collections.nlp.modules.common import SequenceClassifier
from nemo.collections.nlp.modules.common.common_utils import get_pretrained_lm_model
from nemo.core.classes.common import typecheck
from nemo.core.classes.modelPT import ModelPT
from nemo.core.neural_types import NeuralType
from nemo.utils.decorators import experimental

__all__ = ['TextClassificationModel']


@experimental
class TextClassificationModel(ModelPT):
    @property
    def input_types(self) -> Optional[Dict[str, NeuralType]]:
        return self.bert_model.input_types

    @property
    def output_types(self) -> Optional[Dict[str, NeuralType]]:
        return self.classifier.output_types

    def __init__(self, cfg: DictConfig, trainer: Trainer = None):
        """Initializes the BERTTextClassifier model.
        """

        self.max_seq_length = cfg.language_model.max_seq_length
        self.data_dir = cfg.data_dir
        self.tokenizer = get_tokenizer(
            tokenizer_name=cfg.language_model.tokenizer,
            pretrained_model_name=cfg.language_model.pretrained_model_name,
            vocab_file=cfg.language_model.vocab_file,
            tokenizer_model=cfg.language_model.tokenizer_model,
            do_lower_case=cfg.language_model.do_lower_case,
        )

        # init superclass
        super().__init__(cfg=cfg, trainer=trainer)

        self.data_desc = TextClassificationDataDesc(data_dir=self.data_dir, modes=["train", "test", "dev"])

        self.bert_model = get_pretrained_lm_model(
            pretrained_model_name=cfg.language_model.pretrained_model_name, config_file=cfg.language_model.bert_config
        )
        self.hidden_size = self.bert_model.config.hidden_size
        self.classifier = SequenceClassifier(
            hidden_size=self.hidden_size,
            num_classes=self.data_desc.num_classes,
            num_layers=cfg.head.num_output_layers,
            activation='relu',
            log_softmax=False,
            dropout=cfg.head.fc_dropout,
            use_transformer_init=True,
            idx_conditioned_on=0,
        )

        if cfg.class_balancing == 'weighted_loss':
            # You may need to increase the number of epochs for convergence when using weighted_loss
            self.loss = CrossEntropyLoss(weight=self.data_desc.class_weights)
        else:
            self.loss = CrossEntropyLoss()

        # setup to track metrics
        self.classification_report = ClassificationReport(self.data_desc.num_classes)

        # Optimizer setup needs to happen after all model weights are ready
        self.setup_optimization(cfg.optim)

    @typecheck()
    def forward(self, input_ids, token_type_ids, attention_mask):
        """
        No special modification required for Lightning, define it as you normally would
        in the `nn.Module` in vanilla PyTorch.
        """
        hidden_states = self.bert_model(
            input_ids=input_ids, token_type_ids=token_type_ids, attention_mask=attention_mask
        )
        logits = self.classifier(hidden_states=hidden_states)
        return logits

    def training_step(self, batch, batch_idx):
        """
        Lightning calls this inside the training loop with the data from the training dataloader
        passed in as `batch`.
        """
        # forward pass
        input_ids, input_type_ids, input_mask, labels = batch
        logits = self(input_ids=input_ids, token_type_ids=input_type_ids, attention_mask=input_mask)

        # TODO replace with loss module
        train_loss = self.loss(logits=logits, labels=labels)

        tensorboard_logs = {'train_loss': train_loss, 'lr': self._optimizer.param_groups[0]['lr']}
        return {'loss': train_loss, 'log': tensorboard_logs}

    def validation_step(self, batch, batch_idx):
        """
        Lightning calls this inside the validation loop with the data from the validation dataloader
        passed in as `batch`.
        """
        input_ids, input_type_ids, input_mask, labels = batch
        logits = self(input_ids=input_ids, token_type_ids=input_type_ids, attention_mask=input_mask)

        val_loss = self.loss(logits=logits, labels=labels)

        preds = torch.argmax(logits, axis=-1)
        tp, fp, fn = self.classification_report(preds, labels)

        tensorboard_logs = {'val_loss': val_loss, 'tp': tp, 'fn': fn, 'fp': fp}

        return {'val_loss': val_loss, 'log': tensorboard_logs}

    def validation_epoch_end(self, outputs):
        """
        Called at the end of validation to aggregate outputs.
        :param outputs: list of individual outputs of each validation step.
        """
        # TODO: Check why need this?
        if outputs:
            avg_loss = torch.stack([x['val_loss'] for x in outputs]).mean()

            # calculate metrics and log classification report
            tp = torch.sum(torch.stack([x['log']['tp'] for x in outputs]), 0)
            fn = torch.sum(torch.stack([x['log']['fn'] for x in outputs]), 0)
            fp = torch.sum(torch.stack([x['log']['fp'] for x in outputs]), 0)
            precision, recall, f1 = self.classification_report.get_precision_recall_f1(tp, fn, fp, mode='micro')

            tensorboard_logs = {
                'val_loss': avg_loss,
                'precision': precision,
                'recall': recall,
                'f1': f1,
            }
            return {'val_loss': avg_loss, 'log': tensorboard_logs}

    def setup_training_data(self, train_data_config: Optional[DictConfig]):
        self._train_dl = self._setup_dataloader_from_config(cfg=train_data_config)

    def setup_validation_data(self, val_data_config: Optional[DictConfig]):
        self._validation_dl = self._setup_dataloader_from_config(cfg=val_data_config)

    def setup_test_data(self, test_data_config: Optional[DictConfig]):
        self._test_dl = self.__setup_dataloader(cfg=test_data_config)

    def _setup_dataloader_from_config(self, cfg: DictConfig):
        input_file = os.path.join(self.data_dir, f"{cfg.prefix}.tsv")
        # TODO: check that file exists
        dataset = TextClassificationDataset(
            input_file=input_file,
            tokenizer=self.tokenizer,
            max_seq_length=self.max_seq_length,
            num_samples=cfg.num_samples,
            shuffle=cfg.shuffle,
            use_cache=cfg.use_cache,
        )

        return torch.utils.data.DataLoader(
            dataset=dataset,
            batch_size=cfg.batch_size,
            shuffle=cfg.shuffle,
            num_workers=cfg.num_workers,
            pin_memory=cfg.pin_memory,
            drop_last=cfg.drop_last,
        )

    @classmethod
    def save_to(cls, save_path: str):
        """
        Saves the module to the specified path.
        Args:
            :param save_path: Path to where to save the module.
        """
        pass

    @classmethod
    def restore_from(cls, restore_path: str):
        """
        Restores the module from the specified path.
        Args:
            :param restore_path: Path to restore the module from.
        """
        pass

    @classmethod
    def list_available_models(cls) -> Optional[Dict[str, str]]:
        pass

    @classmethod
    def from_pretrained(cls, name: str):
        pass

    @classmethod
    def export(cls, **kwargs):
        pass