"""Gate 4 (fault 3/3) caller: makes real HTTP requests to caption_gen_service.py and
pushes caption_gen_success_rate / caption_gen_error_rate computed from ACTUAL responses
received over the network -- not synthetic values.
"""
import sys
import time
import urllib.error
import urllib.request

PUSHGATEWAY = "http://localhost:9091"
JOB = "media_pipeline_captiongen"
SERVICE_URL = "http://localhost:8090"


def push_sample(success_rate: float, error_rate: float, mode: str, sample_idx: int, layer_up: int):
    body = (
        f"# HELP caption_gen_success_rate Fraction of real HTTP requests to the caption-gen service that succeeded\n"
        f"# TYPE caption_gen_success_rate gauge\n"
        f'caption_gen_success_rate{{layer="captions_upstream",mode="{mode}"}} {success_rate:.4f}\n'
        f"# HELP caption_gen_error_rate Fraction of real HTTP requests that returned an error\n"
        f"# TYPE caption_gen_error_rate gauge\n"
        f'caption_gen_error_rate{{layer="captions_upstream",mode="{mode}"}} {error_rate:.4f}\n'
        f"# HELP layer_up Layer health derived from real request outcomes\n"
        f"# TYPE layer_up gauge\n"
        f'layer_up{{layer="captions",mode="{mode}"}} {layer_up}\n'
    )
    req = urllib.request.Request(
        f"{PUSHGATEWAY}/metrics/job/{JOB}/mode/{mode}", data=body.encode(), method="POST"
    )
    urllib.request.urlopen(req)
    print(f"  sample {sample_idx}: success_rate={success_rate:.2f} error_rate={error_rate:.2f} layer_up={layer_up}")


def make_real_request() -> bool:
    """Returns True if the real HTTP call to the caption-gen stub succeeded."""
    try:
        req = urllib.request.Request(f"{SERVICE_URL}/generate", data=b"{}", method="POST")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except urllib.error.HTTPError:
        return False
    except Exception:
        return False


def run(mode: str, batches: int = 10, requests_per_batch: int = 20):
    for idx in range(batches):
        outcomes = [make_real_request() for _ in range(requests_per_batch)]
        successes = sum(outcomes)
        success_rate = successes / requests_per_batch
        error_rate = 1 - success_rate
        layer_up = 1 if success_rate > 0.5 else 0
        push_sample(success_rate, error_rate, mode, idx, layer_up)
        time.sleep(1)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    print(f"=== making REAL HTTP requests to caption-gen stub, mode={mode} ===")
    run(mode)
