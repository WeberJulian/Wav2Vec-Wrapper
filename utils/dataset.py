import torch
import re
import torchaudio
import librosa
import numpy as np

from datasets import load_dataset, concatenate_datasets
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

from transformers import Wav2Vec2CTCTokenizer
from transformers import Wav2Vec2FeatureExtractor
from transformers import Wav2Vec2Processor

def parse_dataset_dict(data_dict):
    text_column = data_dict['text_column']
    audio_path_column = data_dict['path_column']
    del data_dict['text_column']
    del data_dict['path_column']
    return text_column, audio_path_column


class Dataset(object):
    def __init__(self, config, vocab, text_column='text', audio_path_column='audio_path'):
        self.config = config
        self.text_column = text_column
        self.audio_path_column = audio_path_column
        self.vocab = vocab
        # load datasets
        self.train_dataset = None
        self.devel_dataset = None

        self.tokenizer = Wav2Vec2CTCTokenizer(self.config.vocab['vocab_path'], unk_token=self.config.vocab['unk'], pad_token=self.config.vocab['blank'], word_delimiter_token=self.config.vocab['silence'])
        self.feature_extractor = Wav2Vec2FeatureExtractor(feature_size=1, sampling_rate=self.config['sampling_rate'], padding_value=0.0, do_normalize=True, return_attention_mask=True)
        self.processor = Wav2Vec2Processor(feature_extractor=self.feature_extractor, tokenizer=self.tokenizer)

        # create dataset
        self.initialize_datasets()


    def preprocess_datasets(self):
        # remove all invalid characters present in text
        self.normalise_texts()
        
        self.audio_preprocess_and_prepare_dataset()
        

    def initialize_datasets(self):
        for dataset_dict in self.config.datasets['train']:
            self.train_dataset = self.make_dataset(dataset_dict)

        for dataset_dict in self.config.datasets['devel']:
            self.devel_dataset = self.make_dataset(dataset_dict)

    def remove_extra_and_rename_columns(self, dataset, text_column, audio_path_column):
        # remove unused columns
        remove_column = dataset.column_names
        remove_column.remove(text_column)
        remove_column.remove(audio_path_column)
        dataset = dataset.remove_columns(remove_column)

        # rename columns 
        dataset = dataset.rename_column(text_column, self.text_column)
        dataset = dataset.rename_column(audio_path_column, self.audio_path_column)
        return dataset

    def make_dataset(self, dataset_dict):

        own_dataset = None
        text_column, audio_path_column = parse_dataset_dict(dataset_dict)
        
        dataset = load_dataset(**dataset_dict)
        # remove extra columns
        dataset = self.remove_extra_and_rename_columns(dataset, text_column, audio_path_column)

        if own_dataset is None:
            own_dataset = dataset
        else:
            own_dataset = concatenate_datasets([self.train_dataset, dataset])
        return own_dataset    
        
    def normalise_texts(self):
        vocab_list = list(self.vocab.keys())
        # remove special tokens
        vocab_list.remove(self.config.vocab['blank'])
        vocab_list.remove(self.config.vocab['silence'])
        vocab_list.remove(self.config.vocab['unk'])
        # append space token
        vocab_list.append(' ')
        # convert to string
        vocab_string = ''.join(vocab_list)

        def remove_invalid_characters(batch):
            text = batch[self.text_column].lower()
            text = re.sub("[^{}]".format(vocab_string), " ", text)
            text = re.sub("[ ]+", " ", text)
            
            batch[self.text_column] = text + " "
            return batch

        print("> Prepare Texts")
        # remove invalid chars
        self.train_dataset = self.train_dataset.map(remove_invalid_characters, num_proc=self.config['num_loader_workers'])
        self.devel_dataset = self.devel_dataset.map(remove_invalid_characters, num_proc=self.config['num_loader_workers'])

    def audio_preprocess_and_prepare_dataset(self):

        def read_audio(batch):
            speech_array, sampling_rate = torchaudio.load(batch[self.audio_path_column])
            batch["speech"] = speech_array.squeeze().numpy()
            batch["sampling_rate"] = sampling_rate
            batch["target_text"] = batch[self.text_column]
            return batch

        def resample_audio(batch):
            if batch["sampling_rate"] != self.config['sampling_rate']:
                #speech_array = torchaudio.transforms.Resample(batch["sampling_rate"], self.config['sampling_rate'])(torch.FloatTensor(speech_array).unsqueeze(0)).squeeze().numpy()
                batch["speech"] = librosa.resample(np.asarray(batch["speech"]),  batch["sampling_rate"], self.config['sampling_rate'])
                batch["sampling_rate"] = self.config['sampling_rate']
            return batch
        
        def prepare_dataset(batch):
            batch["input_values"] = self.processor(batch["speech"], sampling_rate=self.config['sampling_rate']).input_values
            with self.processor.as_target_processor():
                batch["labels"] = self.processor(batch["target_text"]).input_ids
            return batch

        print("> Load Audios")
        self.train_dataset = self.train_dataset.map(read_audio, remove_columns=self.train_dataset.column_names)
        self.devel_dataset = self.devel_dataset.map(read_audio, remove_columns=self.devel_dataset.column_names)

        print("> Resample Audios if necessary")
        self.train_dataset = self.train_dataset.map(resample_audio, num_proc=self.config['num_loader_workers'])
        self.devel_dataset = self.devel_dataset.map(resample_audio, num_proc=self.config['num_loader_workers'])
        
        print("> Prepare dataloader")
        self.train_dataset = self.train_dataset.map(prepare_dataset, remove_columns=self.train_dataset.column_names, batch_size=self.config['batch_size'], num_proc=self.config['num_loader_workers'], batched=True)
        self.devel_dataset = self.devel_dataset.map(prepare_dataset, remove_columns=self.devel_dataset.column_names, batch_size=self.config['batch_size'], num_proc=self.config['num_loader_workers'], batched=True)


if __name__ == "__main__":
    from generic_utils import load_config, load_vocab
    config_path = 'example/config_example.json'

    config = load_config(config_path)
    vocab = load_vocab(config.vocab['vocab_path'])
    dataset = Dataset(config, vocab)




@dataclass
class DataColletor:
    # Adpated from https://huggingface.co/blog/fine-tune-xlsr-wav2vec2
    """
    Data collator that will dynamically pad the inputs received.
    Args:
        processor (:class:`~transformers.Wav2Vec2Processor`)
            The processor used for proccessing the data.
        padding (:obj:`bool`, :obj:`str` or :class:`~transformers.tokenization_utils_base.PaddingStrategy`, `optional`, defaults to :obj:`True`):
            Select a strategy to pad the returned sequences (according to the model's padding side and padding index)
            among:
            * :obj:`True` or :obj:`'longest'`: Pad to the longest sequence in the batch (or no padding if only a single
              sequence if provided).
            * :obj:`'max_length'`: Pad to a maximum length specified with the argument :obj:`max_length` or to the
              maximum acceptable input length for the model if that argument is not provided.
            * :obj:`False` or :obj:`'do_not_pad'` (default): No padding (i.e., can output a batch with sequences of
              different lengths).
        max_length (:obj:`int`, `optional`):
            Maximum length of the ``input_values`` of the returned list and optionally padding length (see above).
        max_length_labels (:obj:`int`, `optional`):
            Maximum length of the ``labels`` returned list and optionally padding length (see above).
        pad_to_multiple_of (:obj:`int`, `optional`):
            If set will pad the sequence to a multiple of the provided value.
            This is especially useful to enable the use of Tensor Cores on NVIDIA hardware with compute capability >=
            7.5 (Volta).
    """

    processor: Wav2Vec2Processor
    padding: Union[bool, str] = True
    max_length: Optional[int] = None
    max_length_labels: Optional[int] = None
    pad_to_multiple_of: Optional[int] = None
    pad_to_multiple_of_labels: Optional[int] = None

    def __call__(self, features: List[Dict[str, Union[List[int], torch.Tensor]]]) -> Dict[str, torch.Tensor]:
        # split inputs and labels since they have to be of different lenghts and need
        # different padding methods
        input_features = []
        label_features = []
        for feature in features:
            input_features.append({"input_values": feature["input_values"]})
            label_features.append({"input_ids": feature["labels"]})

        batch = self.processor.pad(
            input_features,
            padding=self.padding,
            max_length=self.max_length,
            pad_to_multiple_of=self.pad_to_multiple_of,
            return_tensors="pt",
        )
        with self.processor.as_target_processor():
            labels_batch = self.processor.pad(
                label_features,
                padding=self.padding,
                max_length=self.max_length_labels,
                pad_to_multiple_of=self.pad_to_multiple_of_labels,
                return_tensors="pt",
            )

        # replace padding with -100 to ignore loss correctly
        labels = labels_batch["input_ids"].masked_fill(labels_batch.attention_mask.ne(1), -100)

        batch["labels"] = labels

        return batch