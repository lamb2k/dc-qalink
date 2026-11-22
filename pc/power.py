#!/usr/bin/env python3
"""power.py: the control loop's last rung, a power cycle of the console (dc-qalink 0.2). Python 3 standard library.

Until a smart plug is named this prints "power cycle needed" and returns False: the person at the console switches it
off and on. One adapter class per plug model, chosen by DCQL_PLUG (model name) with its settings in DCQL_PLUG_ARGS
(JSON), for example DCQL_PLUG=example DCQL_PLUG_ARGS='{"host": "192.168.1.40"}'. A new model is one class with
off() and on(); nothing else in the loop changes.

On a console with a GDEMU, a power cycle starts openMenu, not the loader: the loop closes only if the card boots the
loader's image by itself, or a person picks it once after the cycle (see the owner's setup sheet).

  python power.py            cycle with the configured plug (or say that a person must)
"""
import json, os, sys, time


class Plug:
    off_s = 5.0   # long enough for the console's supply to drain

    def __init__(self, **kw):
        self.kw = kw

    def off(self):
        raise NotImplementedError

    def on(self):
        raise NotImplementedError

    def cycle(self, log):
        log('power: %s off' % self.name)
        self.off()
        time.sleep(self.off_s)
        log('power: %s on' % self.name)
        self.on()
        return True


class NoPlug(Plug):
    name = 'none'

    def cycle(self, log):
        log('power cycle needed: switch the console off, wait 5 s, switch it on '
            '(no smart plug is configured: set DCQL_PLUG once the owner names one)')
        return False


class ExamplePlug(Plug):
    """A template for the first real model (an HTTP plug that switches with one GET). Not a tested adapter."""
    name = 'example'

    def _get(self, path):
        import urllib.request
        urllib.request.urlopen('http://%s%s' % (self.kw['host'], path), timeout=5).read()

    def off(self):
        self._get(self.kw.get('off', '/off'))

    def on(self):
        self._get(self.kw.get('on', '/on'))


PLUGS = {'none': NoPlug, 'example': ExamplePlug}


def plug():
    model = os.environ.get('DCQL_PLUG', 'none')
    args = json.loads(os.environ.get('DCQL_PLUG_ARGS', '{}') or '{}')
    return PLUGS.get(model, NoPlug)(**args)


def cycle(log=print):
    """True when a plug switched the console off and on; False when a person must."""
    return plug().cycle(log)


if __name__ == '__main__':
    sys.exit(0 if cycle(lambda s: print(s, flush=True)) else 6)
