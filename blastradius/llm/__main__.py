"""Check the LLM fallback chain.

    python -m blastradius.llm status     # which slots are configured, is Ollama running
    python -m blastradius.llm ping       # send one tiny prompt through the chain
"""
import sys
import urllib.request

from blastradius import config  # noqa: F401  (loads .env)
from blastradius.llm import AllProvidersExhausted, LLMGateway


def _ollama_running(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(base_url.removesuffix("/v1") + "/api/tags", timeout=2):
            return True
    except OSError:
        return False


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    gw = LLMGateway()
    if cmd == "status":
        print(f"{'#':<3}{'provider':<12}{'state':<30}models")
        for i, p in enumerate(gw.providers, 1):
            if p.local:
                state = "running" if _ollama_running(p.base_url) else "not running (skipped)"
            else:
                state = "ready" if p.configured else "no key (template, skipped)"
            print(f"{i:<3}{p.name:<12}{state:<30}{', '.join(p.models) or '-'}")
    elif cmd == "ping":
        try:
            r = gw.chat([{"role": "user", "content": "Reply with exactly: pong"}], max_tokens=200)
        except AllProvidersExhausted as e:
            print(e)
            for a in e.attempts:
                print("  tried", a)
            sys.exit(1)
        for a in r.attempts:
            print("  fell back from", a)
        print(f"{r.provider}/{r.model} ({r.latency_ms} ms): {(r.message.content or '').strip()}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
