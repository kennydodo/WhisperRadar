"""Small localhost client for the scheduled Codex packaging worker.

Claim tokens stay in the worker's private local state file, not console logs.
This client makes no image/text API calls and imports no project code.
"""
import argparse
import json
from pathlib import Path
import urllib.error
import urllib.request
from urllib.parse import urlparse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("claim", "complete", "fail"))
    parser.add_argument("--url", default="http://127.0.0.1:8540")
    parser.add_argument("--job-file", required=True)
    parser.add_argument("--result-file")
    parser.add_argument("--error")
    args = parser.parse_args()
    if urlparse(args.url).hostname not in ("127.0.0.1", "localhost", "::1"):
        parser.error("The bridge must use the local dashboard")
    path = Path(args.job_file)
    if args.action == "claim":
        endpoint, body = "/gpt-pro/jobs/claim", {}
    else:
        job = json.loads(path.read_text(encoding="utf-8"))
        endpoint = f"/gpt-pro/jobs/{job['id']}/{args.action}"
        body = {"claim_token": job["claim_token"]}
        if args.action == "complete":
            if not args.result_file:
                parser.error("complete requires --result-file")
            body["result"] = json.loads(Path(args.result_file).read_text(encoding="utf-8"))
        else:
            body["error"] = args.error or "Codex could not finish this job"
    req = urllib.request.Request(args.url.rstrip("/") + endpoint,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-WhisperRadar-Agent": "codex-packaging"})
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Dashboard rejected {args.action}: {exc.read().decode()[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Dashboard is unavailable: {exc.reason}") from exc
    if args.action == "claim":
        if data.get("job"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data["job"], ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"job_id": data["job"]["id"],
                              "production_id": data["job"]["context"]["production_id"],
                              "job_file": str(path)}))
        else:
            print('{"job":null}')
    else:
        print(json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    main()
