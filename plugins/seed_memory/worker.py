"""Detached seed queue worker. Run as: python -m seed_memory.worker --once"""
import argparse, fcntl, importlib, logging, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--once', action='store_true'); args=ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s', stream=sys.stdout, force=True)
    home=Path(os.environ.get('HERMES_HOME', Path.home()/'.hermes'))
    cfg=home/'config.yaml'
    text=cfg.read_text(encoding='utf-8') if cfg.exists() else ''
    def val(k):
        for line in text.splitlines():
            if k+':' in line: return line.split(':',1)[1].strip().strip('"')
        return 'default'
    key_present = bool(os.environ.get('LAN_QWEN_API_KEY'))
    print('worker config hermes_home=%s db_path=%s write_enabled=%s api_key_present=%s llm_timeout_s=%s tagger_max_tokens=%s provider=%s' % (home,val('db_path'),val('write_enabled'),key_present,val('llm_timeout_s'),val('tagger_max_tokens'),val('provider')), flush=True)
    lock_path=home/'seed_queue'/'worker.lock'; lock_path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    with open(lock_path,'a+') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return 0
        mod=importlib.import_module('seed_memory')
        p=mod.SeedMemoryProvider()
        p.initialize('detached-worker', hermes_home=str(home), start_worker=False)
        p._drain_queue()
    return 0

def _cli_entry():
    rc = main()
    import sys
    sys.stdout.flush(); sys.stderr.flush()
    os._exit(int(rc or 0))

if __name__=='__main__': _cli_entry()
