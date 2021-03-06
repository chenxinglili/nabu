'''@file dblstm.py
contains de LAS class'''

import tensorflow as tf
from nabu.neuralnetworks.classifiers import classifier
from nabu.neuralnetworks.classifiers.asr.encoders import encoder_factory
from nabu.neuralnetworks.classifiers.asr.asr_decoders import asr_decoder_factory

class EncoderDecoder(classifier.Classifier):
    '''a general class for an encoder decoder system'''
    def __init__(self, conf, output_dim, name=None):
        '''LAS constructor

        Args:
            conf: The classifier configuration
            output_dim: the classifier output dimension
            name: the classifier name
        '''

        super(EncoderDecoder, self).__init__(conf, output_dim, name)

        #create the listener
        self.encoder = encoder_factory.factory(conf)

        #create the speller
        self.decoder = asr_decoder_factory.factory(conf, self.output_dim)

    def _get_outputs(self, inputs, input_seq_length, targets=None,
                     target_seq_length=None, is_training=False):
        '''
        Add the neural net variables and operations to the graph

        Args:
            inputs: the inputs to the neural network, this is a
                [batch_size x max_input_length x feature_dim] tensor
            input_seq_length: The sequence lengths of the input utterances, this
                is a [batch_size] vector
            targets: the targets to the neural network, this is a tuple of
                [batch_size x max_output_length x dim] tensors. The targets can
                be used during training
            target_seq_length: The sequence lengths of the target utterances,
                this is a tuple of [batch_size] vectors
            is_training: whether or not the network is in training mode

        Returns:
            A pair containing:
                - output logits (tuple of two kind of logits)
                - the output logits sequence lengths as tuple of two vectors
        '''

        #add input noise
        std_input_noise = float(self.conf['std_input_noise'])
        if is_training and std_input_noise > 0:
            noisy_inputs = inputs + tf.random_normal(
                inputs.get_shape(), stddev=std_input_noise)
        else:
            noisy_inputs = inputs

        #compute the high level features
        hlfeat = self.encoder(
            inputs=noisy_inputs,
            sequence_lengths=input_seq_length,
            is_training=is_training)

        #prepend a sequence border label to the targets to get the encoder
        #inputs, the label is the last label
        batch_size = int(targets[0].get_shape()[0])
        s_labels = tf.constant(self.output_dim-1,
                               dtype=tf.int32,
                               shape=[batch_size, 1])

        encoder_inputs = tf.concat([s_labels, targets[0]], 1)

        #compute the output logits
        logits, _ = self.decoder(
            hlfeat=hlfeat,
            encoder_inputs=encoder_inputs,
            initial_state=self.decoder.zero_state(batch_size),
            first_step=True,
            is_training=is_training)

        # adapt the sequence length of the logits in the correct way
        # plus one if the target length was not zero because and eos label will
        # be added, remain zero when the target length was also zero.
        empty_targets = tf.equal(target_seq_length[0], 0)
        zeros = tf.zeros([target_seq_length[0].get_shape()[0]], dtype=tf.int32)
        logit_seq_length = tf.where(empty_targets, zeros,
                                    target_seq_length[0]+1)

        return (logits, None), (logit_seq_length, None)
