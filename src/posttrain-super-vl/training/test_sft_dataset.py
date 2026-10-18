"""Check sft_dataset.py against exported data and the real Super VL tokenizer / chat template (CPU only).

1. The rebuilt first prompt (system + tools + user + generation prompt) must have the same number of tokens as vLLM
   counted for the run's first request (transcript step 1) - an exact check that system prompt, tool schemas and user
   prompt were reconstructed as served.
2. Labels cover only assistant turns, start right after the assistant header (and the empty think block), end with
   <|im_end|>, and every tool call is supervised.
usage: python test_sft_dataset.py --tokenizer /data/models/super-vl-bf16 --data /data/sft/<run> [--n 100]
       [--transcripts ROOT]   (eval-bench root; only on the Jupyter VM, where the transcripts live)
"""
import argparse
import json
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import sft_dataset as sd  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tokenizer", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--file", default="train.jsonl")
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--transcripts", default=None)
    a = ap.parse_args()
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(a.tokenizer, trust_remote_code=True)
    rows = [json.loads(l) for l in open(pathlib.Path(a.data) / a.file)]
    random.Random(0).shuffle(rows)
    rows = rows[:a.n]
    exact = diffs = 0
    if a.transcripts:
        for r in rows:
            meta = json.loads(r["meta"])
            first = None
            for line in open(pathlib.Path(a.transcripts) / meta["source"] / "transcript.json"):
                e = json.loads(line) if line.startswith("{") else {}
                if e.get("type") == "step_finish":
                    first = e["part"]["tokens"]["input"]
                    break
            prompt = tok.apply_chat_template(sd.to_template_messages(r["messages"][:2]), tools=json.loads(r["tools"]),
                                             tokenize=False, add_generation_prompt=True)
            n = len(tok(prompt, add_special_tokens=False)["input_ids"])
            exact += n == first
            diffs += abs(n - first)
            if n != first and diffs < 200:
                print(f"  {r['id']}: rebuilt {n} vs served {first}")
        print(f"first-prompt token count identical to vLLM's: {exact}/{len(rows)} (mean abs diff {diffs / len(rows):.1f})")
    batch = sd.agent_collate_fn(rows[:4], tok, max_length=None, pad_to_multiple=128)
    marker = tok.encode(sd.ASSISTANT_MARKER, add_special_tokens=False)
    end_id = tok.convert_tokens_to_ids(sd.END)
    sup = 0
    for b, r in enumerate(rows[:4]):
        lab = batch["labels"][b]
        ids = batch["input_ids"][b]
        assert ids.shape == lab.shape
        n_calls = sum(len(m.get("tool_calls") or []) for m in r["messages"] if m["role"] == "assistant")
        n_turns = sum(m["role"] == "assistant" for m in r["messages"])
        text = tok.decode([t for t in lab.tolist() if t != -100])
        assert text.count("<tool_call>") == n_calls, (r["id"], text.count("<tool_call>"), n_calls)
        assert lab.tolist().count(end_id) == n_turns, (r["id"], lab.tolist().count(end_id), n_turns)
        assert "<|im_start|>" not in text and "<think></think>" not in text
        ids_l = ids.tolist()
        first_marker = next(i for i in range(len(ids_l)) if ids_l[i:i + len(marker)] == marker)
        first_sup = next(i for i, t in enumerate(lab.tolist()) if t != -100)
        assert first_sup >= first_marker + len(marker) - 1, (first_sup, first_marker)  # labels are shifted by one
        tool_resp = tok.encode("<tool_response>", add_special_tokens=False)
        assert tok.decode(tool_resp) not in text  # tool results are never supervised
        sup += int((lab != -100).sum())
        full = int(batch["attention_mask"][b].sum())
        print(f"  {r['id']}: {full} tokens (export said {json.loads(r['meta'])['n_tokens']}), supervised {int((lab != -100).sum())}, "
              f"assistant turns {n_turns}, tool calls {n_calls}")
    print("labels OK; marker", marker, "end", end_id)


if __name__ == "__main__":
    main()
