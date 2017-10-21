from __future__ import print_function, division

"""
Online decoder using frequency features.

TODO:
Allow self.ref_new to be a list.

TODO:
Use Pathos to simplify daemon class.
(Should be free from non-picklable limitation.)

Auhtor:
Kyuhwa Lee
Swiss Federal Institute of Technology Lausanne (EPFL)

"""

import pycnbi.utils.pycnbi_utils as pu
import pycnbi.utils.q_common as qc
import time
import os
import sys
import random
import pdb
import numpy as np
import multiprocessing as mp
import multiprocessing.sharedctypes as sharedctypes

def get_decoder_info(classifier):
    """
    Get only the classifier information without connecting to a server

    Params
    ------
        classifier: model file

    Returns
    -------
        info dictionary object
    """

    model = qc.load_obj(classifier)
    if model == None:
        print('>> Error loading %s' % model)
        sys.exit(-1)

    cls = model['cls']
    psde = model['psde']
    labels = list(cls.classes_)
    w_seconds = model['w_seconds']
    w_frames = model['w_frames']
    wstep = model['wstep']
    sfreq = model['sfreq']
    psd_temp = psde.transform(np.zeros((1, len(model['picks']), w_frames)))
    psd_shape = psd_temp.shape
    psd_size = psd_temp.size

    info = dict(labels=labels, cls=cls, psde=psde, w_seconds=w_seconds, w_frames=w_frames,\
                wstep=wstep, sfreq=sfreq, psd_shape=psd_shape, psd_size=psd_size)
    return info


class BCIDecoder(object):
    """
    Decoder class

    The label order of self.labels and self.label_names match likelihood orders computed by get_prob()

    """

    def __init__(self, classifier=None, buffer_size=1.0, fake=False, amp_serial=None, amp_name=None):
        """
        Params
        ------
            classifier: classifier file
            spatial: spatial filter to use
            buffer_size: length of the signal buffer in seconds
        """

        from pycnbi.stream_receiver.stream_receiver import StreamReceiver

        self.classifier = classifier
        self.buffer_sec = buffer_size
        self.fake = fake
        self.amp_serial = amp_serial
        self.amp_name = amp_name

        if self.fake == False:
            model = qc.load_obj(self.classifier)
            if model == None:
                self.print('Error loading %s' % model)
                sys.exit(-1)
            self.cls = model['cls']
            self.psde = model['psde']
            self.labels = list(self.cls.classes_)
            self.label_names = [model['classes'][k] for k in self.labels]
            self.spatial = model['spatial']
            self.spectral = model['spectral']
            self.notch = model['notch']
            self.w_seconds = model['w_seconds']
            self.w_frames = model['w_frames']
            self.wstep = model['wstep']
            self.sfreq = model['sfreq']
            if not int(self.sfreq * self.w_seconds) == self.w_frames:
                raise RuntimeError('sfreq * w_sec %d != w_frames %d' % (int(self.sfreq * self.w_seconds), self.w_frames))

            if 'multiplier' in model:
                self.multiplier = model['multiplier']
            else:
                self.multiplier = 1

            # Stream Receiver
            self.sr = StreamReceiver(window_size=self.w_seconds, amp_name=self.amp_name, amp_serial=self.amp_serial)
            if self.sfreq != self.sr.sample_rate:
                raise RuntimeError('Amplifier sampling rate (%.1f) != model sampling rate (%.1f). Stop.' % (
                    self.sr.sample_rate, self.sfreq))

            # Map channel indices based on channel names of the streaming server
            self.spatial_ch = model['spatial_ch']
            self.spectral_ch = model['spectral_ch']
            self.notch_ch = model['notch_ch']
            self.ref_new = model['ref_new']
            self.ref_old = model['ref_old']
            self.ch_names = self.sr.get_channel_names()
            mc = model['ch_names']
            self.picks = [self.ch_names.index(mc[p]) for p in model['picks']]
            if self.spatial_ch is not None:
                self.spatial_ch = [self.ch_names.index(mc[p]) for p in model['spatial_ch']]
            if self.spectral_ch is not None:
                self.spectral_ch = [self.ch_names.index(mc[p]) for p in model['spectral_ch']]
            if self.notch_ch is not None:
                self.notch_ch = [self.ch_names.index(mc[p]) for p in model['notch_ch']]
            if self.ref_new is not None:
                self.ref_new = self.ch_names.index(mc[model['ref_new']])
            if self.ref_old is not None:
                self.ref_old = self.ch_names.index(mc[model['ref_old']])

            # PSD buffer
            psd_temp = self.psde.transform(np.zeros((1, len(self.picks), self.w_frames)))
            self.psd_shape = psd_temp.shape
            self.psd_size = psd_temp.size
            self.psd_buffer = np.zeros((0, self.psd_shape[1], self.psd_shape[2]))
            self.ts_buffer = []

        else:
            # Fake left-right decoder
            model = None
            self.psd_shape = None
            self.psd_size = None
            from pycnbi.triggers.trigger_def import trigger_def
            # TODO: parameterize directions using fake_dirs
            self.labels = [11, 9]
            self.label_names = {'LEFT_GO':11, 'RIGHT_GO':9}

    def print(self, *args):
        if len(args) > 0: print('[BCIDecoder] ', end='')
        print(*args)

    def get_labels(self):
        """
        Returns
        -------
            Class labels numbers in the same order as the likelihoods returned by get_prob()
        """
        return self.labels

    def get_label_names(self):
        """
        Returns
        -------
            Class label names in the same order as get_labels()
        """
        return self.label_names

    def start(self):
        pass

    def stop(self):
        pass

    def get_prob(self):
        """
        Read the latest window

        Returns
        -------
            The likelihood P(X|C), where X=window, C=model
        """
        if self.fake:
            # fake deocder: biased likelihood for the first class
            probs = [random.uniform(0.0, 1.0)]
            # others class likelihoods are just set to equal
            p_others = (1 - probs[0]) / (len(self.labels) - 1)
            for x in range(1, len(self.labels)):
                probs.append(p_others)
            time.sleep(0.0625)  # simulated delay for PSD + RF on Rex laptop
        else:
            self.sr.acquire()
            w, ts = self.sr.get_window()  # w = times x channels
            w = w.T  # -> channels x times

            # apply filters. Important: maintain the original channel order at this point.
            pu.preprocess(w, sfreq=self.sfreq, spatial=self.spatial, spatial_ch=self.spatial_ch,
                          spectral=self.spectral, spectral_ch=self.spectral_ch, notch=self.notch,
                          notch_ch=self.notch_ch, multiplier=self.multiplier)

            # select the same channels used for training
            w = w[self.picks]

            # debug: show max - min
            # c=1; print( '### %d: %.1f - %.1f = %.1f'% ( self.picks[c], max(w[c]), min(w[c]), max(w[c])-min(w[c]) ) )

            # psd = channels x freqs
            psd = self.psde.transform(w.reshape((1, w.shape[0], w.shape[1])))

            # update psd buffer ( < 1 msec overhead )
            self.psd_buffer = np.concatenate((self.psd_buffer, psd), axis=0)
            self.ts_buffer.append(ts[0])
            if ts[0] - self.ts_buffer[0] > self.buffer_sec:
                # search speed comparison for ordered arrays:
                # http://stackoverflow.com/questions/16243955/numpy-first-occurence-of-value-greater-than-existing-value
                t_index = np.searchsorted(self.ts_buffer, ts[0] - 1.0)
                self.ts_buffer = self.ts_buffer[t_index:]
                self.psd_buffer = self.psd_buffer[t_index:, :, :]  # numpy delete is slower
            # assert ts[0] - self.ts_buffer[0] <= self.buffer_sec

            # make a feautre vector and classify
            feats = np.concatenate(psd[0]).reshape(1, -1)
            probs = self.cls.predict_proba(feats)[0]

        return probs

    def get_prob_unread(self):
        return self.get_prob()

    def get_psd(self):
        """
        Returns
        -------
            The latest computed PSD
        """
        return self.psd_buffer[-1].reshape((1, -1))

    def is_ready(self):
        """
        Ready to decode? Returns True if buffer is not empty.
        """
        return self.sr.is_ready()


class BCIDecoderDaemon(object):
    """
    BCI Decoder daemon class

    Some codes are redundant because BCIDecoder class object cannot be pickled.
    BCIDecoder object must be created inside the child process.

    Constructor params:
        classifier: file name of the classifier
        buffer_size: buffer window size in seconds
        fake:
            False= Connect to an amplifier LSL server and decode
            True=  Create a mock decoder (fake probabilities biased to 1.0)
        fake_dirs:
            example: ['LEFT_GO', 'RIGHT_GO']
    """

    def __init__(self, classifier=None, buffer_size=1.0, fake=False, amp_serial=None, amp_name=None, fake_dirs=None):
        """
        Params
        ------
            classifier: classifier file.
            buffer_size: buffer size in seconds.
        """

        self.classifier = classifier
        self.buffer_sec = buffer_size
        self.startmsg = 'Decoder daemon started.'
        self.stopmsg = 'Decoder daemon stopped.'
        self.fake = fake
        self.amp_serial = amp_serial
        self.amp_name = amp_name

        if fake == False or fake is None:
            self.model = qc.load_obj(self.classifier)
            if self.model == None:
                raise IOError('Error loading %s' % self.model)
            else:
                self.labels = self.model['cls'].classes_
                self.label_names = {self.model['classes'][k]:k for k in self.model['cls'].classes_}
        else:
            # create a fake decoder with LEFT/RIGHT classes
            self.model = None
            from pycnbi.triggers.trigger_def import trigger_def
            tdef = trigger_def('triggerdef_16.ini')
            if type(fake_dirs) is not list:
                raise RuntimeError('Decoder(): wrong argument type of fake_dirs: %s.' % type(fake_dirs))
            self.labels = [tdef.by_key[t] for t in fake_dirs]
            self.label_names = [tdef.by_value[v] for v in self.labels]
            self.startmsg = '** WARNING: FAKE ' + self.startmsg
            self.stopmsg = 'FAKE ' + self.stopmsg

        self.psdlock = mp.Lock()
        self.reset()
        self.start()

    def print(self, *args):
        if len(args) > 0: print('[BCIDecoderDaemon] ', end='')
        print(*args)

    def reset(self):
        """
        Reset classifier to the initial state
        """
        # share numpy array self.psd between processes.
        # to compute the shared memory size, we need to create a temporary decoder object.
        if self.fake == True:
            psd_size = None
            psd_shape = None
            psd_ctypes = None
            self.psd = None
        else:
            info = get_decoder_info(self.classifier)
            psd_size = info['psd_size']
            psd_shape = info['psd_shape'][1:]  # we get only the last window
            psd_ctypes = sharedctypes.RawArray('d', np.zeros(psd_size))
            self.psd = np.frombuffer(psd_ctypes, dtype=np.float64, count=psd_size)

        self.probs = mp.Array('d', [1.0 / len(self.labels)] * len(self.labels))
        self.pread = mp.Value('i', 1)
        self.running = mp.Value('i', 0)
        self.return_psd = mp.Value('i', 0)
        mp.freeze_support()
        self.proc = mp.Process(target=self.daemon, args=\
            [self.classifier, self.probs, self.pread, self.running, self.return_psd, psd_ctypes, self.psdlock])

    def daemon(self, classifier, probs, pread, running, return_psd, psd_ctypes, lock):
        """
        The whole decoder loop runs on a child process because
        BCIDecoder object cannot be pickled.
        """
        from numpy import ctypeslib

        decoder = BCIDecoder(classifier, buffer_size=self.buffer_sec,\
                             fake=self.fake, amp_serial=self.amp_serial, amp_name=self.amp_name)
        if self.fake == False:
            psd = ctypeslib.as_array(psd_ctypes)
        else:
            psd = None

        self.running.value = 1
        while running.value == 1:
            probs[:] = decoder.get_prob()
            pread.value = 0

            if self.fake == False:
                '''
                Copy back PSD values only when needed.
                We don't need a lock (return_psd.value acts as a lock).
                '''
                if return_psd.value == 1:
                    # lock.acquire()
                    psd[:] = decoder.psd_buffer[-1].reshape((1, -1))
                    # lock.release()
                    return_psd.value = 0

    def start(self):
        """
        Start the daemon
        """
        if self.running.value == 1:
            self.print('ERROR: Cannot start. Daemon already running. (PID %d)' % self.proc.pid)
            return
        self.proc.start()
        self.print(self.startmsg)

    def stop(self):
        """
        Stop the daemon
        """
        if self.running.value == 0:
            self.print('Warning: Decoder already stopped.')
            return
        self.running.value = 0
        self.proc.join()
        self.reset()
        self.print(self.stopmsg)

    def get_labels(self):
        """
        Returns
        -------
            Class labels numbers in the same order as the likelihoods returned by get_prob()
        """
        return self.labels

    def get_label_names(self):
        """
        Returns
        -------
            Class label names in the same order as get_labels()
        """
        return self.label_names

    def get_prob(self):
        """
        Returns
        -------
            The last computed probability.
        """
        self.pread.value = 1
        return self.probs[:]

    def get_prob_unread(self):
        """
        Returns
        -------
            Probability if it's not read previously.
            None otherwise.
        """
        if self.pread.value == 0:
            return self.get_prob()
        else:
            return None

    def get_psd(self):
        """
        Returns
        -------
            The latest computed PSD
        """
        self.return_psd.value = 1
        while self.return_psd.value == 1:
            time.sleep(0.001)
        return self.psd

    def is_running(self):
        return self.running.value

# test decoding speed
def check_speed(model_file, amp_name=None, amp_serial=None, max_count=float('inf')):
    decoder = BCIDecoder(model_file, buffer_size=1.0, amp_name=amp_name, amp_serial=amp_serial)
    tm = qc.Timer()
    count = 0
    count_total = 0
    mslist = []
    while True:
        if count_total >= max_count:
            break
        decoder.get_prob()
        count += 1
        count_total += 1
        if tm.sec() > 1:
            t = tm.sec()
            ms = 1000*t/count
            # show time per classification and its reciprocal
            print('%.0f ms/c   %.1f Hz' % (ms, count/t))
            mslist.append(ms)
            count = 0
            tm.reset()
    print('mean = %.1f ms' % np.mean(mslist))

# sample decoding code
def sample_decoding(model_file, buffer_size=1.0, amp_name=None, amp_serial=None):
    # run on background
    # decoder= BCIDecoderDaemon(model_file, buffer_size=1.0, fake=False, amp_name=amp_name, amp_serial=amp_serial)

    # run on foreground
    decoder = BCIDecoder(model_file, buffer_size=1.0, amp_name=amp_name, amp_serial=amp_serial)

    # run a fake classifier on background
    # decoder= BCIDecoderDaemon(fake=True, fake_dirs=['L','R'])


    # load trigger definitions for labeling
    labels = decoder.get_label_names()
    probs = [0.5] * len(labels)  # integrator

    tm_watchdog = qc.Timer(autoreset=True)
    tm_cls = qc.Timer()
    while True:
        praw = decoder.get_prob_unread()
        if praw is None:
            # watch dog
            if tm_cls.sec() > 5:
                print('WARNING: No classification was done in the last 5 seconds. Are you receiving data streams?')
                tm_cls.reset()
            tm_watchdog.sleep_atleast(0.001)
            continue

        print('[%8.1f msec]' % (tm_cls.msec()), end='')
        for i in range(len(labels)):
            probs[i] = probs[i] * 0.8 + praw[i] * 0.2
            print('   %s %.3f (raw %.3f)' % (labels[i], probs[i], praw[i]), end='')
        maxi = qc.get_index_max(probs)
        print('   ', labels[maxi])
        tm_cls.reset()

# sample code
if __name__ == '__main__':
    model_file = r'D:\data\CHUV\ECoG17\20171005\fif_corrected\stepblocks\classifier_steps\classifier-64bit.pkl'

    if len(sys.argv) == 2:
        amp_name = sys.argv[1]
        amp_serial = None
    elif len(sys.argv) == 3:
        amp_name, amp_serial = sys.argv[1:3]
    else:
        amp_name, amp_serial = pu.search_lsl(ignore_markers=True)
    if amp_name == 'None':
        amp_name = None
    print('Connecting to a server %s (Serial %s).' % (amp_name, amp_serial))

    check_speed(model_file, amp_name, amp_serial, 1000)
    #sample_decoding(model_file, amp_name=amp_name, amp_serial=amp_serial)