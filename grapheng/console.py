import hashlib
import json
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ._store import (
    atomic_json_write,
    atomic_text_write,
    file_lock,
    read_json_object,
)
from .checkpoint import CheckpointStore
from .errors import ContractViolation
from .events import JsonlEventSink
from .learning import RSILoop
from .model import GraphSpec
from .optimization import RSIOptimizationLab
from .policy import AllowListGatePolicy


@dataclass(frozen=True)
class ApprovalItem:
    run_id: str
    node_id: str
    gate: str
    status: str
    actor: Optional[str] = None
    note: Optional[str] = None
    decided_at: Optional[float] = None


class ApprovalInbox:
    def __init__(self, control_root: Path, state_root: Optional[Path] = None):
        self.control_root = control_root
        self.control_root.mkdir(parents=True, exist_ok=True)
        self.state_root = state_root or control_root / "approvals"
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.control_root / ".approval-decisions.lock"
        self.lock_path.touch(exist_ok=True)

    def list(self, run_id: Optional[str] = None) -> Tuple[ApprovalItem, ...]:
        if run_id is not None:
            _validate_run_id(run_id)
        run_dirs = (
            (self.control_root / "runs" / run_id,)
            if run_id is not None
            else tuple(sorted((self.control_root / "runs").glob("*")))
        )
        decisions = self._decisions()
        items = []
        for run_dir in run_dirs:
            if not run_dir.is_dir():
                continue
            graph = _read_graph(run_dir)
            statuses = _read_statuses(run_dir)
            run_decisions = decisions.get(run_dir.name, {})
            for node in graph.nodes:
                if node.gate is None or statuses.get(node.id) != "blocked":
                    continue
                decision = run_decisions.get(node.gate, {})
                items.append(
                    ApprovalItem(
                        run_id=run_dir.name,
                        node_id=node.id,
                        gate=node.gate,
                        status=str(decision.get("decision", "pending")),
                        actor=decision.get("actor"),
                        note=decision.get("note"),
                        decided_at=decision.get("decided_at"),
                    )
                )
        return tuple(sorted(items, key=lambda item: (item.run_id, item.gate, item.node_id)))

    def decide(
        self,
        run_id: str,
        gate: str,
        decision: str,
        actor: str,
        note: Optional[str] = None,
    ) -> ApprovalItem:
        _validate_run_id(run_id)
        if not isinstance(gate, str) or not gate.strip():
            raise ContractViolation("approval gate cannot be empty")
        if decision not in ("allow", "deny"):
            raise ContractViolation("approval decision must be allow or deny")
        if not isinstance(actor, str) or not actor.strip():
            raise ContractViolation("approval actor cannot be empty")
        if note is not None and not isinstance(note, str):
            raise ContractViolation("approval note must be a string")
        with self._locked():
            pending = [item for item in self.list(run_id) if item.gate == gate]
            if not pending:
                raise ContractViolation(f"run {run_id} has no blocked gate {gate}")
            decisions = self._decisions()
            decisions.setdefault(run_id, {})[gate] = {
                "decision": decision,
                "actor": actor.strip(),
                "note": note,
                "decided_at": time.time(),
            }
            atomic_json_write(self.state_root / "decisions.json", decisions)
            return next(item for item in self.list(run_id) if item.gate == gate)

    def policy_for(self, run_id: str) -> AllowListGatePolicy:
        _validate_run_id(run_id)
        decisions = self._decisions().get(run_id, {})
        return AllowListGatePolicy(
            {gate for gate, value in decisions.items() if value.get("decision") == "allow"}
        )

    def _decisions(self) -> Dict[str, Any]:
        path = self.state_root / "decisions.json"
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ContractViolation(f"invalid approval state: {error}") from error
        if not isinstance(value, dict):
            raise ContractViolation("approval state must be an object")
        return value

    @contextmanager
    def _locked(self):
        with file_lock(self.lock_path):
            yield


class OperationsConsole:
    """Builds one stable operations projection and a self-contained local UI."""

    def __init__(
        self,
        control_root: Path,
        approval_inbox: Optional[ApprovalInbox] = None,
        learning_root: Optional[Path] = None,
        optimization_root: Optional[Path] = None,
        agent_os: Optional[Any] = None,
    ):
        self.control_root = control_root
        self.approvals = approval_inbox or ApprovalInbox(control_root)
        self.learning_root = learning_root
        self.optimization_root = optimization_root
        self.agent_os = agent_os

    def snapshot(self, run_id: str) -> Mapping[str, Any]:
        _validate_run_id(run_id)
        run_dir = self.control_root / "runs" / run_id
        if not run_dir.is_dir():
            raise ContractViolation(f"run {run_id} does not exist")
        graph = _read_graph(run_dir)
        state = read_json_object(run_dir / "state.json")
        checkpoint = CheckpointStore(run_dir / "runtime" / "checkpoint.json").load()
        event_path = run_dir / "runtime" / "events.jsonl"
        events = tuple(JsonlEventSink(event_path).read()) if event_path.exists() else ()
        statuses = _read_statuses(run_dir)
        attempts = checkpoint.attempts if checkpoint is not None else {}
        completed = {
            event["node_id"]: event
            for event in events
            if event.get("event") == "node_completed" and event.get("node_id")
        }
        nodes = []
        for node in graph.nodes:
            event = completed.get(node.id, {})
            payload = event.get("payload", {})
            metadata = payload.get("metadata", {})
            cost = payload.get("cost_usd")
            if cost is None:
                cost = metadata.get("cost_usd")
            nodes.append(
                {
                    "id": node.id,
                    "kind": node.kind,
                    "deps": list(node.deps),
                    "status": statuses.get(node.id, "pending"),
                    "gate": node.gate,
                    "attempts": int(attempts.get(node.id, 0)),
                    "tokens_used": payload.get("tokens_used", 0),
                    "cost_usd": cost,
                    "executor_id": metadata.get("executor_id"),
                    "reuse_status": metadata.get("reuse_status"),
                }
            )
        readers: Dict[str, List[str]] = {}
        for node in graph.nodes:
            for key in node.reads:
                readers.setdefault(key, []).append(node.id)
        artifacts = []
        if checkpoint is not None:
            for record in checkpoint.artifacts:
                artifacts.append(
                    {
                        "key": record.key,
                        "producer": record.producer,
                        "consumers": sorted(readers.get(record.key, [])),
                        "version": record.version,
                        "checksum": record.checksum,
                    }
                )
        replay = [
            event
            for event in events
            if event.get("event")
            in ("node_blocked", "node_failed", "node_retry", "node_cancelled")
        ]
        publications = []
        publication_path = run_dir / "runtime" / "verified-publications.json"
        if publication_path.exists():
            publication_state = read_json_object(publication_path)
            for item in publication_state.get("publications", ()):
                if not isinstance(item, dict):
                    raise ContractViolation(
                        "verified-publications.json contains an invalid publication"
                    )
                publications.append(
                    {
                        "publication_id": item.get("publication_id"),
                        "source_node_id": item.get("node_id"),
                        "status": item.get("status"),
                        "verifier_id": item.get("verifier_id"),
                        "quality_score": item.get("quality_score"),
                        "reason": item.get("reason"),
                    }
                )
        result = state.get("result") or {}
        rsi = self._rsi_snapshot()
        optimization = self._optimization_snapshot()
        return {
            "run": {
                "run_id": run_id,
                "graph_id": graph.id,
                "phase": state.get("phase"),
                "generation": state.get("generation", 1),
                "tokens_used": result.get(
                    "tokens_used", checkpoint.tokens_used if checkpoint else 0
                ),
                "cost_usd": result.get(
                    "cost_usd", checkpoint.cost_usd if checkpoint else 0.0
                ),
                "submitted_at": state.get("submitted_at"),
                "heartbeat_at": state.get("heartbeat_at"),
                "event_cursor": len(events),
            },
            "nodes": nodes,
            "edges": [
                {"from": dependency, "to": node.id}
                for node in graph.nodes
                for dependency in node.deps
            ],
            "artifacts": artifacts,
            "approvals": [asdict(item) for item in self.approvals.list(run_id)],
            "replay": replay,
            "publications": publications,
            "rsi": rsi,
            "optimization": optimization,
            "agent_os": self.agent_os.status() if self.agent_os is not None else None,
        }

    def changes(
        self,
        run_id: str,
        revision: Optional[str] = None,
        after: int = 0,
    ) -> Mapping[str, Any]:
        """Returns a conditional snapshot plus cursor-based event changes."""
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ContractViolation("console event cursor must be non-negative")
        snapshot = self.snapshot(run_id)
        event_path = self.control_root / "runs" / run_id / "runtime" / "events.jsonl"
        events = tuple(JsonlEventSink(event_path).read()) if event_path.exists() else ()
        if after > len(events):
            raise ContractViolation("console event cursor is ahead of the run")
        encoded = json.dumps(
            snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        current_revision = hashlib.sha256(encoded).hexdigest()
        changed = revision != current_revision
        return {
            "changed": changed,
            "revision": current_revision,
            "next_cursor": len(events),
            "events": list(events[after:]),
            "snapshot": snapshot if changed else None,
        }

    def render(self, run_id: str, output: Path) -> Path:
        payload = json.dumps(
            self.snapshot(run_id), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).replace("</", "<\\/")
        html = _HTML.replace("__GRAPHENG_PAYLOAD__", payload)
        atomic_text_write(output, html)
        return output

    def _rsi_snapshot(self) -> Mapping[str, Any]:
        if self.learning_root is None:
            return {"active_policy": None, "candidates": []}
        loop = RSILoop(self.learning_root)
        active = loop.active_policy()
        return {
            "active_policy": active.to_dict() if active is not None else None,
            "candidates": [candidate.to_dict() for candidate in loop.candidates()],
        }

    def _optimization_snapshot(self) -> Mapping[str, Any]:
        if self.optimization_root is None:
            return {"active": {}, "candidates": []}
        lab = RSIOptimizationLab(self.optimization_root)
        return {
            "active": {
                kind: candidate.to_dict()
                for kind, candidate in lab.active_candidates().items()
            },
            "candidates": [candidate.to_dict() for candidate in lab.candidates()],
        }


def _read_graph(run_dir: Path) -> GraphSpec:
    try:
        return GraphSpec.from_dict(read_json_object(run_dir / "graph.json"))
    except ContractViolation:
        raise
    except Exception as error:
        raise ContractViolation(f"cannot read run graph: {error}") from error


def _read_statuses(run_dir: Path) -> Mapping[str, str]:
    state = read_json_object(run_dir / "state.json")
    result = state.get("result") or {}
    if isinstance(result.get("statuses"), dict):
        return result["statuses"]
    checkpoint = CheckpointStore(run_dir / "runtime" / "checkpoint.json").load()
    return checkpoint.statuses if checkpoint is not None else {}


def _validate_run_id(run_id: str) -> None:
    if (
        not isinstance(run_id, str)
        or not run_id
        or run_id in (".", "..")
        or Path(run_id).name != run_id
    ):
        raise ContractViolation("run id must be one path segment")


_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent OS 控制台</title><style>
:root{color-scheme:dark;--bg:#0a0f1e;--panel:#121a2c;--line:#26324b;--text:#e8eefc;--muted:#91a0bc;--ok:#43d17d;--bad:#ff6b6b;--wait:#f9c74f;--accent:#70a5ff}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top,#182341,var(--bg) 42%);font:14px/1.5 ui-sans-serif,system-ui;color:var(--text)}main{max-width:1240px;margin:auto;padding:28px}h1{font-size:26px;margin:0 0 6px}.sub{color:var(--muted);margin-bottom:22px}.metrics,.grid{display:grid;gap:14px}.metrics{grid-template-columns:repeat(4,1fr)}.grid{grid-template-columns:1.3fr 1fr;margin-top:14px}.panel,.metric{background:color-mix(in srgb,var(--panel) 92%,transparent);border:1px solid var(--line);border-radius:14px;padding:16px}.metric b{display:block;font-size:22px;margin-top:5px}.panel h2{font-size:16px;margin:0 0 12px}.nodes{display:grid;gap:9px}.node{border-left:4px solid var(--accent);background:#0d1425;padding:10px 12px;border-radius:8px}.node.completed{border-color:var(--ok)}.node.failed,.node.blocked,.node.cancelled{border-color:var(--bad)}.node small,.muted{color:var(--muted)}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:500}.pill{display:inline-block;padding:2px 8px;border-radius:99px;background:#23304a}.pill.ok{color:var(--ok)}.pill.bad{color:var(--bad)}pre{white-space:pre-wrap;word-break:break-word;background:#0b1120;padding:10px;border-radius:8px;max-height:260px;overflow:auto}.wide{grid-column:1/-1}@media(max-width:800px){.metrics,.grid{grid-template-columns:1fr 1fr}}@media(max-width:520px){.metrics,.grid{grid-template-columns:1fr}}
</style></head><body><main><h1>Agent OS 控制台</h1><div class="sub" id="subtitle"></div><section class="metrics" id="metrics"></section><section class="grid"><div class="panel"><h2>运行图</h2><div class="nodes" id="nodes"></div></div><div class="panel"><h2>审批收件箱</h2><div id="approvals"></div></div><div class="panel"><h2>Artifact 血缘</h2><div id="artifacts"></div></div><div class="panel"><h2>RSI 学习状态</h2><div id="rsi"></div></div><div class="panel wide"><h2>验证结果发布</h2><div id="publications"></div></div><div class="panel wide"><h2>失败回放</h2><div id="replay"></div></div></section></main><script>
const d=__GRAPHENG_PAYLOAD__,run=d.run;document.getElementById('subtitle').textContent=`${run.graph_id} · ${run.run_id}`;
const fmt=x=>x===null||x===undefined?'—':x,esc=x=>String(fmt(x)).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));document.getElementById('metrics').innerHTML=[['状态',run.phase],['代数',run.generation],['Token',run.tokens_used],['费用','$'+Number(run.cost_usd||0).toFixed(4)]].map(x=>`<div class="metric"><span class="muted">${esc(x[0])}</span><b>${esc(x[1])}</b></div>`).join('');
document.getElementById('nodes').innerHTML=d.nodes.map(n=>`<div class="node ${esc(n.status)}"><b>${esc(n.id)}</b> <span class="pill">${esc(n.status)}</span><br><small>${esc(n.kind)} · 依赖: ${n.deps.length?n.deps.map(esc).join(', '):'无'} · 执行器: ${esc(n.executor_id)} · 复用: ${esc(n.reuse_status)} · $${Number(n.cost_usd||0).toFixed(4)}</small></div>`).join('');
const table=(heads,rows)=>rows.length?`<table><thead><tr>${heads.map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table>`:'<span class="muted">暂无</span>';
document.getElementById('approvals').innerHTML=table(['Gate','节点','状态'],d.approvals.map(a=>`<tr><td>${esc(a.gate)}</td><td>${esc(a.node_id)}</td><td><span class="pill ${a.status==='allow'?'ok':a.status==='deny'?'bad':''}">${esc(a.status)}</span></td></tr>`));
document.getElementById('artifacts').innerHTML=table(['Artifact','生产者','消费者'],d.artifacts.map(a=>`<tr><td>${esc(a.key)} v${esc(a.version)}</td><td>${esc(a.producer)}</td><td>${a.consumers.length?a.consumers.map(esc).join(', '):'—'}</td></tr>`));
const active=d.rsi.active_policy,opt=Object.values(d.optimization.active||{}),reuse=d.agent_os&&d.agent_os.reuse;document.getElementById('rsi').innerHTML=(active?`<p><span class="pill ok">路由已激活</span> ${esc(active.version)}</p><p class="muted">灰度 ${esc(active.rollout_percent)}% · ${Object.keys(active.estimates).length} 个执行器模型</p>`:'<p class="muted">尚无已激活路由策略</p>')+(opt.length?opt.map(x=>`<p><span class="pill ok">优化已激活</span> ${esc(x.kind)} · 灰度 ${esc(x.rollout_percent)}%</p>`).join(''):'<p class="muted">尚无已激活提示/拓扑优化</p>')+(reuse?`<p><span class="pill ok">安全复用</span> ${esc(reuse.entries.valid)} 个有效结果 · 节省 ${esc(reuse.saved_tokens)} Token / ${reuse.saved_cost_complete?'$'+Number(reuse.saved_cost_usd).toFixed(4):'费用 unknown'}</p>`:'');
document.getElementById('publications').innerHTML=table(['来源','验证器','状态','质量/原因'],d.publications.map(p=>`<tr><td>${esc(p.source_node_id)}</td><td>${esc(p.verifier_id)}</td><td><span class="pill ${p.status==='published'?'ok':p.status==='skipped'?'bad':''}">${esc(p.status)}</span></td><td>${p.quality_score===null||p.quality_score===undefined?esc(p.reason):esc(p.quality_score)}</td></tr>`));
document.getElementById('replay').innerHTML=d.replay.length?d.replay.map(e=>`<pre>${esc(e.time)} · ${esc(e.event)} · ${esc(e.node_id)}\n${esc(JSON.stringify(e.payload,null,2))}</pre>`).join(''):'<span class="muted">本次运行没有失败事件</span>';
</script></body></html>"""
