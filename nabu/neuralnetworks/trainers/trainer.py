'''@file trainer.py
neural network trainer environment'''

import os
from abc import ABCMeta, abstractmethod
from time import time, sleep
import tensorflow as tf
import numpy as np

class Trainer(object):
    '''General class outlining the training environment of a classifier.'''
    __metaclass__ = ABCMeta

    def __init__(self,
                 conf,
                 decoder,
                 classifier,
                 input_dim,
                 reconstruction_dim,
                 dispenser,
                 val_reader,
                 val_targets,
                 expdir,
                 server,
                 task_index):
        '''
        NnetTrainer constructor, creates the training graph

        Args:
            classifier: the neural net classifier that will be trained
            conf: the trainer config
            decoder: a callable that will create a decoder
            input_dim: the input dimension to the nnnetgraph
            reconstruction_dim: dimension of the reconstruction targets
            dispenser: a Batchdispenser object
            val_reader: the feature reader for the validation data if None
                validation will not be used
            val_targets: a dictionary containing the targets of the validation
                set
            expdir: directory where the summaries will be written
            server: optional server to be used for distributed training
            task_index: optional index of the worker task in the cluster
        '''

        self.conf = conf
        self.dispenser = dispenser
        self.num_steps = int(dispenser.num_batches*int(conf['num_epochs'])
                             /max(1, int(conf['numbatches_to_aggregate'])))
        self.val_reader = val_reader
        self.val_targets = val_targets

        self.expdir = expdir
        self.server = server
        cluster = tf.train.ClusterSpec(server.server_def.cluster)

        #save the max lengths
        self.max_target_length1, self.max_target_length2 =\
            dispenser.max_target_length
        self.max_input_length = dispenser.max_input_length

        # save the boolean that holds if doing learning rate adaptation
        if 'learning_rate_adaptation' in conf:
            if conf['learning_rate_adaptation'] == 'True':
                self.learning_rate_adaptation = True
            elif conf['learning_rate_adaptation'] == 'False':
                self.learning_rate_adaptation = False
            else:
                raise Exception('wrong kind of info in \
                    learning_rate_adaptation')
        else:
        # if not specified, assum learning rate adaptation is not necessary
            self.learning_rate_adaptation = False

        #create the graph
        self.graph = tf.Graph()

        if 'local' in cluster.as_dict():
            num_replicas = 1
        else:
            #distributed training
            num_replicas = len(cluster.as_dict()['worker'])

        self.is_chief = task_index == 0
        device = tf.train.replica_device_setter(
            cluster=cluster,
            worker_device='/job:worker/task:%d' % task_index)

        #define the placeholders in the graph
        with self.graph.as_default():

            with tf.device(device):

                #create the inputs placeholder
                self.inputs = tf.placeholder(
                    dtype=tf.float32,
                    shape=[dispenser.size, self.max_input_length,
                           input_dim],
                    name='inputs')

                #the first part of the tupple of targets (text targets)
                targets1 = tf.placeholder(
                    dtype=tf.int32,
                    shape=[dispenser.size, self.max_target_length1],
                    name='targets1')

                #second part of the tupple of targets
                #(audio samples or input features)
                targets2 = tf.placeholder(
                    dtype=tf.float32,
                    shape=[dispenser.size, self.max_target_length2,
                           reconstruction_dim],
                    name='targets2')

                # the targets are passed together as a tupple
                self.targets = (targets1, targets2)

                #the length of all the input sequences
                self.input_seq_length = tf.placeholder(
                    dtype=tf.int32,
                    shape=[dispenser.size],
                    name='input_seq_length')

                #length of all the output sequences (first from target tuple)
                target_seq_length1 = tf.placeholder(
                    dtype=tf.int32,
                    shape=[dispenser.size],
                    name='output_seq_length1')

                #length of the sequences of the second element of target tuple
                target_seq_length2 = tf.placeholder(
                    dtype=tf.int32,
                    shape=[dispenser.size],
                    name='output_seq_length2')

                # last two placeholders are passed together as one argument
                self.target_seq_length = \
                    (target_seq_length1, target_seq_length2)

                #a placeholder to set the position
                self.pos_in = tf.placeholder(
                    dtype=tf.int32,
                    shape=[],
                    name='pos_in')

                self.val_loss_in = tf.placeholder(
                    dtype=tf.float32,
                    shape=[],
                    name='val_loss_in')

                #compute the training outputs of the classifier
                trainlogits, logit_seq_length = classifier(
                    inputs=self.inputs,
                    input_seq_length=self.input_seq_length,
                    targets=self.targets,
                    target_seq_length=self.target_seq_length,
                    is_training=True)

                #create a decoder object for validation
                if self.conf['validation_mode'] == 'decode':
                    self.decoder = decoder()
                elif self.conf['validation_mode'] == 'loss':
                    vallogits, val_logit_seq_length = classifier(
                        inputs=self.inputs,
                        input_seq_length=self.input_seq_length,
                        targets=self.targets,
                        target_seq_length=self.target_seq_length,
                        is_training=False)

                    self.decoder_loss = self.compute_loss(
                        self.targets, vallogits, val_logit_seq_length,
                        self.target_seq_length)
                else:
                    raise Exception('unknown validation mode %s' %
                                    self.conf['validation_mode'])


                #a variable to hold the amount of steps already taken
                self.global_step = tf.get_variable(
                    name='global_step',
                    shape=[],
                    dtype=tf.int32,
                    initializer=tf.constant_initializer(0),
                    trainable=False)

                with tf.variable_scope('train'):

                    #a variable that indicates if features are being read
                    self.reading = tf.get_variable(
                        name='reading',
                        shape=[],
                        dtype=tf.bool,
                        initializer=tf.constant_initializer(False),
                        trainable=False)

                    #the position in the feature reader
                    self.pos = tf.get_variable(
                        name='position',
                        shape=[],
                        dtype=tf.int32,
                        initializer=tf.constant_initializer(0),
                        trainable=False)

                    #the current validation loss
                    self.val_loss = tf.get_variable(
                        name='validation_loss',
                        shape=[],
                        dtype=tf.float32,
                        initializer=tf.constant_initializer(1.79e+308),
                        trainable=False)

                    #a variable that specifies when the model was last validated
                    self.validated_step = tf.get_variable(
                        name='validated_step',
                        shape=[],
                        dtype=tf.int32,
                        initializer=tf.constant_initializer(
                            -int(conf['valid_frequency'])),
                        trainable=False)

                    #operation to start reading
                    self.block_reader = self.reading.assign(True).op

                    #operation to release the reader
                    self.release_reader = self.reading.assign(False).op

                    #operation to set the position
                    self.set_pos = self.pos.assign(self.pos_in).op

                    #operation to update the validated steps
                    self.set_val_step = self.validated_step.assign(
                        self.global_step).op

                    #operation to set the validation loss
                    self.set_val_loss = self.val_loss.assign(
                        self.val_loss_in).op

                    #a variable to scale the learning rate (used to reduce the
                    #learning rate in case validation performance drops)
                    learning_rate_fact = tf.get_variable(
                        name='learning_rate_fact',
                        shape=[],
                        initializer=tf.constant_initializer(1.0),
                        trainable=False)

                    #operation to half the learning rate
                    self.halve_learningrate_op = learning_rate_fact.assign(
                        learning_rate_fact/2).op

                    #factor to scale the learning rate according to how many
                    # of the elements in a batch are equal to zero, if we are
                    # using scaled learning rate
                    if self.learning_rate_adaptation:
                        empty_factor = tf.get_variable(
                            name='empty_targets_factor',
                            shape=[],
                            initializer=tf.constant_initializer(1.0),
                            trainable=False)

                        empty_targets = tf.equal(self.target_seq_length[0], 0)
                        ones = tf.ones(dispenser.size)
                        zeros = tf.zeros(dispenser.size)
                        binary = tf.where(empty_targets, zeros, ones)
                        how_many_not_empty = tf.reduce_sum(binary)
                        empty_factor_new = how_many_not_empty/dispenser.size

                        self.update_emptyfactor_op = empty_factor.assign(
                            empty_factor_new).op
                    else:
                        empty_factor = 1

                    #compute the learning rate with exponential decay and scale
                    #with the learning rate factor
                    self.learning_rate = (tf.train.exponential_decay(
                        learning_rate=float(conf['initial_learning_rate']),
                        global_step=self.global_step,
                        decay_steps=self.num_steps,
                        decay_rate=float(conf['learning_rate_decay']))
                                          * learning_rate_fact * empty_factor)

                    #create the optimizer
                    if 'optimizer' in conf:
                    # we can explicitly specify to use gradient descent
                        if conf['optimizer'] == 'gradient_descent':
                            optimizer = tf.train.GradientDescentOptimizer(
                                self.learning_rate)
                        elif conf['optimizer'] == 'adam':
                        # or to use adam
                            if 'beta1' in conf and 'beta2' in conf:
                                # in which case we can also adapt the params
                                optimizer = tf.train.AdamOptimizer(
                                    learning_rate=self.learning_rate,
                                    beta1=float(conf['beta1']),
                                    beta2=float(conf['beta2'])
                                )
                            else:
                            # if params not specified, use default
                                optimizer = tf.train.AdamOptimizer(
                                    learning_rate=self.learning_rate)
                        else:
                            raise Exception('The trainer ' + conf['optimizer'] \
                                + ' is not defined.')
                    else:
                        # default is adam with standard params
                        optimizer = tf.train.AdamOptimizer(self.learning_rate)

                    #create an optimizer that aggregates gradients
                    if int(conf['numbatches_to_aggregate']) > 0:
                        optimizer = tf.train.SyncReplicasOptimizer(
                            opt=optimizer,
                            replicas_to_aggregate=int(
                                conf['numbatches_to_aggregate']),
                            total_num_replicas=num_replicas)


                    #compute the loss
                    self.loss = self.compute_loss(
                        self.targets, trainlogits, logit_seq_length,
                        self.target_seq_length)

                    #compute the gradients
                    grads = optimizer.compute_gradients(self.loss)

                    with tf.variable_scope('clip'):
                        #clip the gradients
                        grads = [(tf.clip_by_value(grad, -1., 1.), var)
                                 for grad, var in grads]

                    #opperation to apply the gradients
                    apply_gradients_op = optimizer.apply_gradients(
                        grads_and_vars=grads,
                        global_step=self.global_step,
                        name='apply_gradients')

                    #all remaining operations with the UPDATE_OPS GraphKeys
                    update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)

                    #create an operation to update the gradients, the batch_loss
                    #and do all other update ops
                    self.update_op = tf.group(
                        *([apply_gradients_op] + update_ops),
                        name='update')

                #create the summaries for visualisation
                tf.summary.scalar('validation loss', self.val_loss)
                tf.summary.scalar('learning rate', self.learning_rate)

                #create a histogram for all trainable parameters
                for param in tf.trainable_variables():
                    tf.summary.histogram(param.name, param)

                #create the schaffold
                self.scaffold = tf.train.Scaffold()

    @abstractmethod
    def compute_loss(self, targets, logits, logit_seq_length,
                     target_seq_length):
        '''
        Compute the loss

        Creates the operation to compute the loss, this is specific to each
        trainer

        Args:
            targets: a tupple of targets, the first one being a
                [batch_size, max_target_length] tensor containing the real text
                targets, the second one being a [batch_size, max_length x dim]
                tensor containing the reconstruction features.
            logits: a tuple of [batch_size, max_logit_length, dim] tensors
                containing the logits for the text and the reconstruction
            logit_seq_length: the length of all the logit sequences as a tuple
                of [batch_size] vectors
            target_seq_length: the length of all the target sequences as a
                tuple of two [batch_size] vectors, both for one of the elements
                in the targets tuple

        Returns:
            a scalar value containing the total loss
        '''

        raise NotImplementedError('Abstract method')

    def train(self):
        '''train the model'''

        #look for the master if distributed training is done
        master = self.server.target

        #start the session and standart servises
        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        config.allow_soft_placement = True
        #config.log_device_placement = True

        #create a hook for saving the final model
        save_hook = SaveAtEnd(os.path.join(self.expdir, 'model',
                                           'network.ckpt'))

        with self.graph.as_default():
            with tf.train.MonitoredTrainingSession(
                master=master,
                is_chief=self.is_chief,
                checkpoint_dir=os.path.join(self.expdir, 'logdir'),
                scaffold=self.scaffold,
                chief_only_hooks=[save_hook],
                config=config) as sess:

                #set the reading flag to false
                sess.run(self.release_reader)

                #start the training loop
                #pylint: disable=E1101
                while (not sess.should_stop()
                       and self.global_step.eval(sess) < self.num_steps):

                    #check if validation is due
                    [step, val_step] = sess.run(
                        [self.global_step, self.validated_step])
                    if (step - val_step >= int(self.conf['valid_frequency'])
                            and int(self.conf['valid_frequency']) > 0):

                        self.validate(sess)

                    #start time
                    start = time()

                    #wait until the reader is free
                    #pylint: disable=E1101
                    while self.reading.eval(sess):
                        sleep(1)

                    #block the reader
                    sess.run(self.block_reader)

                    #read a batch of data
                    #batch_target_tupples is a list of tupples
                    #pylint: disable=E1101
                    batch_data, batch_labels = self.dispenser.get_batch(
                        self.pos.eval(sess))

                    #update the position
                    self.set_pos.run(
                        session=sess,
                        feed_dict={self.pos_in:self.dispenser.pos})

                    #release the reader
                    sess.run(self.release_reader)

                    #update the model
                    loss, lr = self.update(batch_data, batch_labels, sess)


                    print(('step %d/%d loss: %f, learning rate: %f, '
                           'time elapsed: %f sec')
                          %(self.global_step.eval(sess), self.num_steps,
                            loss, lr, time()-start))

                #the chief will create the final model
                if self.is_chief:
                    if not os.path.isdir(os.path.join(self.expdir, 'model')):
                        os.mkdir(os.path.join(self.expdir, 'model'))

    def update(self, inputs, targets, sess):
        '''
        update the neural model with a batch or training data

        Args:
            inputs: the inputs to the neural net, this should be a list
                containing an NxF matrix for each utterance in the batch where
                N is the number of frames in the utterance
            targets: the targets for neural net, should be a list of tuples,
                each tuple containing two N-dimensional vectors for one
                utterance
            sess: the session

        Returns:
            a pair containing:
                - the loss at this step
                - the learning rate used at this step
        '''

        # go from a list of tupples to two seperate lists
        targets1 = [t[0] for t in targets]
        targets2 = [t[1] for t in targets]

        #get a list of sequence lengths
        input_seq_length = [i.shape[0] for i in inputs]
        target_seq_length1 = [t1.shape[0] for t1 in targets1]
        target_seq_length2 = [t2.shape[0] for t2 in targets2]

        #pad the inputs and targets untill the maximum lengths
        padded_inputs = np.array(pad(inputs, self.max_input_length))
        padded_targets1 = np.array(pad(targets1, self.max_target_length1))
        padded_targets2 = np.array(pad(targets2, self.max_target_length2))

        # first do an update of the emptyness factor
        if self.learning_rate_adaptation:
            _ = sess.run(
                fetches=[self.update_emptyfactor_op],
                feed_dict={self.inputs:padded_inputs,
                           self.targets[0]:padded_targets1,
                           self.targets[1]:padded_targets2,
                           self.input_seq_length:input_seq_length,
                           self.target_seq_length[0]:target_seq_length1,
                           self.target_seq_length[1]:target_seq_length2})

        _, loss, lr = sess.run(
            fetches=[self.update_op,
                     self.loss,
                     self.learning_rate],
            feed_dict={self.inputs:padded_inputs,
                       self.targets[0]:padded_targets1,
                       self.targets[1]:padded_targets2,
                       self.input_seq_length:input_seq_length,
                       self.target_seq_length[0]:target_seq_length1,
                       self.target_seq_length[1]:target_seq_length2})

        return loss, lr

    def validate(self, sess):
        '''
        Evaluate the performance of the neural net and halves the learning rate
        if it is worse

        Args:
            inputs: the inputs to the neural net, this should be a list
                containing NxF matrices for each utterance in the batch where
                N is the number of frames in the utterance
            targets: the one-hot encoded targets for neural net, this should be
            a list containing an NxO matrix for each utterance where O is
                the output dimension of the neural net

        '''

        #update the validated step
        sess.run([self.set_val_step])

        if self.conf['validation_mode'] == 'decode':
            outputs = self.decoder.decode(self.val_reader, sess)

            #when decoding, we want the targets to be only the text targets
            val_text_targets = dict()
            for utt_id in self.val_targets:
                val_text_targets[utt_id] = self.val_targets[utt_id][0]


            val_loss = self.decoder.score(outputs, val_text_targets)

        elif self.conf['validation_mode'] == 'loss':
            val_loss = self.compute_val_loss(self.val_reader, self.val_targets,
                                             sess)
        else:
            raise Exception(self.conf['validation_mode']+' is not a correct\
                choice for the validation mode')

        print 'validation loss: %f' % val_loss

        #pylint: disable=E1101
        if (val_loss > self.val_loss.eval(session=sess)
                and self.conf['valid_adapt'] == 'True'):
            print 'halving learning rate'
            sess.run([self.halve_learningrate_op])

        sess.run(self.set_val_loss, feed_dict={self.val_loss_in:val_loss})

    def compute_val_loss(self, reader, targets, sess):
        '''compute the validation loss on a set in the reader

        Args:
            reader: a reader to read the data
            targets: the ground truth targets as a dictionary
            sess: a tensorflow session

        Returns:
            the loss'''

        looped = False
        avrg_loss = 0.0
        total_elements = 0
        total_steps = int(np.ceil(float(reader.num_utt)/\
            float(self.dispenser.size)))
        step = 1

        while not looped:
            inputs = []
            labels = []

            for _ in range(self.dispenser.size):
                #read a batch of data
                (utt_id, inp, looped) = reader.get_utt()

                inputs.append(inp)
                labels.append(targets[utt_id])

                if looped:
                    break

            num_elements = len(inputs)

            #add empty elements to the inputs to get a full batch
            feat_dim = inputs[0].shape[1]
            if labels[0][1] is not None:
                rec_dim = labels[0][1].shape[1]
            else:
                rec_dim = 1
            inputs += [np.zeros([0, feat_dim])]*(
                self.dispenser.size-len(inputs))
            labels += [np.zeros([0]), np.zeros([0, rec_dim])]*(
                self.dispenser.size-len(labels))

            #get the sequence length
            input_seq_length = [inp.shape[0] for inp in inputs]
            label_seq_length1 = [lab[0].shape[0] for lab in labels]
            label_seq_length2 = [lab[1].shape[0] if lab[1] is not None \
                else 0 for lab in labels]
            #pad and put in a tensor
            input_tensor = np.array([np.append(
                inp, np.zeros([self.max_input_length-inp.shape[0],
                               inp.shape[1]]), 0) for inp in inputs])
            label_tensor1 = np.array([np.append(
                lab[0], np.zeros([self.max_target_length1-lab[0].shape[0]]), 0)
                                      for lab in labels])
            if labels[0][1] is not None:
                label_tensor2 = np.array([np.append(
                    lab[1], np.zeros([self.max_target_length2-lab[1].shape[0],
                                      lab[1].shape[1]]), 0)
                                          for lab in labels])
            else:
                label_tensor2 = np.zeros([self.dispenser.size, 1, 1])
            print 'Doing validation, step %d/%d' %(step, total_steps)

            loss = sess.run(
                self.decoder_loss,
                feed_dict={self.inputs:input_tensor,
                           self.input_seq_length:input_seq_length,
                           self.targets[0]:label_tensor1,
                           self.target_seq_length[0]:label_seq_length1,
                           self.targets[1]:label_tensor2,
                           self.target_seq_length[1]:label_seq_length2})

            avrg_loss = ((total_elements*avrg_loss + num_elements*loss)/
                         (num_elements + total_elements))
            total_elements += num_elements

            step = step+1

        return avrg_loss


def pad(inputs, length):
    '''
    Pad the inputs so they have the maximum length

    Args:
        inputs: the inputs, this should be a list containing time major
            tensors
        length: the length that will be used for padding the inputs

    Returns:
        the padded inputs
    '''
    padded_inputs = [np.append(
        i, np.zeros([length-i.shape[0]] + list(i.shape[1:])), 0)
                     for i in inputs]

    return padded_inputs

class SaveAtEnd(tf.train.SessionRunHook):
    '''a training hook for saving the final model'''

    def __init__(self, filename):
        '''hook constructor

        Args:
            filename: where the model will be saved'''

        self.filename = filename

    def begin(self):
        '''this will be run at session creation'''

        #pylint: disable=W0201
        self._saver = tf.train.Saver(tf.trainable_variables(), sharded=True)

    def end(self, session):
        '''this will be run at session closing'''

        self._saver.save(session, self.filename)
