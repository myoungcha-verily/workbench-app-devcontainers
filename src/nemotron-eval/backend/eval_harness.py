import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

import ollama_client
from models import EvalResult, EvalRun, EvalTask
from store import Store

APP_ROOT = Path(os.environ.get("APP_ROOT", Path(__file__).resolve().parent.parent))
TASKS_FILE = APP_ROOT / "tasks" / "benchmark_tasks.json"
RESULTS_BUCKET = os.environ.get("RESULTS_BUCKET", "")


class EvalHarness:
    def __init__(self, store: Store):
        self.store = store

    def load_tasks(self) -> List[EvalTask]:
        with open(TASKS_FILE) as f:
            data = json.load(f)
        return [EvalTask(**t) for t in data.get("tasks", [])]

    async def run_model(self, model_id: str, prompt: str) -> Dict[str, Any]:
        start = time.time()
        try:
            response = await ollama_client.chat(model_id, [{"role": "user", "content": prompt}])
            return {
                # Reasoning models can spend the whole budget thinking; keep that
                # visible rather than recording an empty answer.
                "output": response["content"] or response["thinking"],
                "latency_ms": int((time.time() - start) * 1000),
                "tokens": response["tokens"],
                "success": bool(response["content"]),
                "error": None if response["content"] else "No final answer (reasoning only or empty)",
            }
        except Exception as e:
            return {
                "output": "",
                "latency_ms": int((time.time() - start) * 1000),
                "tokens": 0,
                "success": False,
                "error": str(e),
            }

    def score_output(self, task: EvalTask, output: str) -> Dict[str, float]:
        scores = {}
        if task.expected_output:
            scores["exact_match"] = 1.0 if output.strip() == task.expected_output.strip() else 0.0
        scores["length"] = min(len(output) / 1000, 1.0)
        scores["has_code"] = 1.0 if "```" in output else 0.0
        return scores

    async def run_eval(self, run: EvalRun, task_ids: List[str]):
        tasks = self.load_tasks()
        run.tasks = [t for t in tasks if t.id in task_ids] if task_ids else tasks
        run.total = len(run.tasks) * len(run.model_ids)
        run.status = "running"
        self.store.save_run(run)

        try:
            # Model-major order: each model is loaded into VRAM once, not once per task.
            for model_id in run.model_ids:
                for task in run.tasks:
                    data = await self.run_model(model_id, task.prompt)
                    self.store.save_result(
                        EvalResult(
                            run_id=run.id,
                            task_id=task.id,
                            model_id=model_id,
                            scores=self.score_output(task, data["output"]),
                            **data,
                        )
                    )
                    run.completed += 1
                    self.store.save_run(run)
            run.status = "completed"
        except Exception as e:
            print(f"[eval] run {run.id} failed: {e}", flush=True)
            run.status = "failed"
        self.store.save_run(run)

        if RESULTS_BUCKET:
            try:
                await asyncio.to_thread(_upload_run, run.id, self.store.get_run(run.id))
            except Exception as e:
                print(f"[eval] upload of run {run.id} to gs://{RESULTS_BUCKET} failed: {e}", flush=True)


def _upload_run(run_id: str, run: Dict[str, Any]):
    from google.cloud import storage

    blob = storage.Client().bucket(RESULTS_BUCKET).blob(f"nemotron-eval/runs/{run_id}.json")
    blob.upload_from_string(json.dumps(run, default=str, indent=2), content_type="application/json")
