import hmac
import json
import secrets
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping, Optional
from urllib.parse import parse_qs, urlsplit

from .console import OperationsConsole
from .errors import ContractViolation

MAX_REQUEST_BYTES = 16 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}


class OperationsAPI:
    """Transport-neutral live projection and single-source approval boundary."""

    def __init__(
        self,
        console: OperationsConsole,
        run_id: str,
        action_token: Optional[str] = None,
    ):
        token = action_token or secrets.token_urlsafe(32)
        if not isinstance(token, str) or len(token) < 16:
            raise ContractViolation("operations action token is too short")
        console.snapshot(run_id)
        self.console = console
        self.run_id = run_id
        self.action_token = token

    def snapshot(
        self, revision: Optional[str] = None, after: int = 0
    ) -> Mapping[str, Any]:
        return self.console.changes(
            self.run_id, revision=revision, after=after
        )

    def approve(self, supplied_token: str, value: Any) -> Mapping[str, Any]:
        if not hmac.compare_digest(supplied_token, self.action_token):
            raise PermissionError("invalid_action_token")
        if not isinstance(value, dict):
            raise ContractViolation("approval request must be an object")
        unknown = set(value) - {"gate", "decision", "actor", "note"}
        if unknown:
            raise ContractViolation("approval request has unknown fields")
        for required in ("gate", "decision", "actor"):
            if not isinstance(value.get(required), str):
                raise ContractViolation(f"approval {required} must be a string")
        note = value.get("note")
        if note is not None and not isinstance(note, str):
            raise ContractViolation("approval note must be a string")
        item = self.console.approvals.decide(
            self.run_id,
            value["gate"],
            value["decision"],
            value["actor"],
            note,
        )
        return {
            "approval": asdict(item),
            "update": self.snapshot(),
        }

    def document(self) -> str:
        initial = self.snapshot()
        payload = json.dumps(
            initial, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).replace("</", "<\\/")
        token = json.dumps(self.action_token).replace("</", "<\\/")
        return _LIVE_HTML.replace("__INITIAL__", payload).replace("__TOKEN__", token)


class OperationsServer:
    """Loopback-only live console; all decisions delegate to ApprovalInbox."""

    def __init__(
        self,
        console: OperationsConsole,
        run_id: str,
        host: str = "127.0.0.1",
        port: int = 0,
        action_token: Optional[str] = None,
    ):
        if host not in LOOPBACK_HOSTS:
            raise ContractViolation("operations server must bind to loopback")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ContractViolation("operations server port must be between 0 and 65535")
        self.api = OperationsAPI(console, run_id, action_token)
        self._httpd = ThreadingHTTPServer((host, port), self._handler_type())
        self._httpd.daemon_threads = True
        self._thread: Optional[threading.Thread] = None

    @property
    def address(self):
        return self._httpd.server_address

    @property
    def action_token(self) -> str:
        return self.api.action_token

    @property
    def url(self) -> str:
        host, port = self.address[:2]
        return f"http://{host}:{port}/"

    def serve_forever(self) -> None:
        self._httpd.serve_forever(poll_interval=0.2)

    def start_in_thread(self) -> threading.Thread:
        if self._thread is not None and self._thread.is_alive():
            raise ContractViolation("operations server is already running")
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        return self._thread

    def shutdown(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _handler_type(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "GraphEngineeringOperations/1"

            def do_GET(self):
                try:
                    parsed = urlsplit(self.path)
                    if parsed.path == "/":
                        self._send_html(owner.api.document())
                        return
                    if parsed.path == "/api/snapshot":
                        query = parse_qs(parsed.query, keep_blank_values=True)
                        revision = _single_query(query, "revision")
                        raw_after = _single_query(query, "after")
                        after = int(raw_after) if raw_after else 0
                        self._send_json(
                            200,
                            owner.api.snapshot(revision=revision, after=after),
                        )
                        return
                    self._send_json(404, {"error": "not_found"})
                except (ContractViolation, ValueError) as error:
                    self._send_json(400, {"error": str(error)})
                except Exception:
                    self._send_json(500, {"error": "internal_server_error"})

            def do_POST(self):
                try:
                    if urlsplit(self.path).path != "/api/approval":
                        self._send_json(404, {"error": "not_found"})
                        return
                    content_type = self.headers.get("Content-Type", "")
                    if not content_type.lower().startswith("application/json"):
                        self._send_json(415, {"error": "application_json_required"})
                        return
                    raw_length = self.headers.get("Content-Length")
                    if raw_length is None:
                        self._send_json(411, {"error": "content_length_required"})
                        return
                    length = int(raw_length)
                    if length < 0 or length > MAX_REQUEST_BYTES:
                        self._send_json(413, {"error": "request_too_large"})
                        return
                    value = json.loads(self.rfile.read(length).decode("utf-8"))
                    self._send_json(
                        200,
                        owner.api.approve(
                            self.headers.get("X-Grapheng-Action-Token", ""),
                            value,
                        ),
                    )
                except PermissionError:
                    self._send_json(403, {"error": "invalid_action_token"})
                except (ContractViolation, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                    self._send_json(400, {"error": str(error)})
                except Exception:
                    self._send_json(500, {"error": "internal_server_error"})

            def do_OPTIONS(self):
                self._send_json(405, {"error": "method_not_allowed"})

            def _send_html(self, value: str) -> None:
                data = value.encode("utf-8")
                self.send_response(200)
                self._security_headers("text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send_json(self, status: int, value: Mapping[str, Any]) -> None:
                data = json.dumps(
                    value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
                self.send_response(status)
                self._security_headers("application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _security_headers(self, content_type: str) -> None:
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; script-src 'unsafe-inline'; "
                    "style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
                )

            def log_message(self, format, *args):
                return

        return Handler

def _single_query(query, name: str) -> Optional[str]:
    values = query.get(name)
    if values is None:
        return None
    if len(values) != 1:
        raise ContractViolation(f"query parameter {name} must appear once")
    return values[0]


_LIVE_HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent OS 实时操作台</title><style>
:root{color-scheme:dark;--bg:#09101d;--panel:#121c2e;--line:#2a3852;--text:#edf3ff;--muted:#91a2bf;--ok:#4bd184;--bad:#ff6f75;--wait:#f4c95d;--accent:#74a7ff}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top,#1a2949,var(--bg) 45%);font:14px/1.5 ui-sans-serif,system-ui;color:var(--text)}main{max-width:1280px;margin:auto;padding:26px}h1{margin:0;font-size:26px}.sub{color:var(--muted);margin:4px 0 18px}.metrics,.grid{display:grid;gap:12px}.metrics{grid-template-columns:repeat(5,1fr)}.grid{grid-template-columns:1.2fr 1fr;margin-top:12px}.panel,.metric{background:rgba(18,28,46,.94);border:1px solid var(--line);border-radius:13px;padding:15px}.metric b{display:block;font-size:20px;margin-top:4px}.panel h2{font-size:16px;margin:0 0 10px}.wide{grid-column:1/-1}.node{border-left:4px solid var(--accent);background:#0c1526;padding:9px 11px;border-radius:8px;margin:7px 0}.node.completed{border-color:var(--ok)}.node.failed,.node.blocked,.node.cancelled{border-color:var(--bad)}.muted,small{color:var(--muted)}.pill{display:inline-block;border-radius:99px;background:#22314c;padding:2px 8px}.ok{color:var(--ok)}.bad{color:var(--bad)}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line)}input{background:#0c1526;color:var(--text);border:1px solid var(--line);border-radius:7px;padding:7px;max-width:150px}button{border:0;border-radius:7px;padding:7px 10px;margin:2px;color:#08111e;background:var(--accent);cursor:pointer}button.deny{background:var(--bad)}#notice{min-height:24px}.flash{color:var(--ok)}.error{color:var(--bad)}@media(max-width:850px){.metrics{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:1fr}.wide{grid-column:auto}}
</style></head><body><main><h1>Agent OS 实时操作台</h1><div class="sub" id="subtitle"></div><div id="notice"></div><section class="metrics" id="metrics"></section><section class="grid"><div class="panel"><h2>任务图</h2><div id="nodes"></div></div><div class="panel"><h2>统一审批收件箱</h2><div id="approvals"></div></div><div class="panel"><h2>Artifact</h2><div id="artifacts"></div></div><div class="panel"><h2>复用与并发合并</h2><div id="reuse"></div></div><div class="panel wide"><h2>验证发布</h2><div id="publications"></div></div><div class="panel wide"><h2>最新事件</h2><div id="events"></div></div></section></main><script>
const actionToken=__TOKEN__;let update=__INITIAL__,revision=update.revision,cursor=update.next_cursor,recent=update.events||[],snapshot=update.snapshot;
const esc=x=>String(x===null||x===undefined?'—':x).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const table=(h,r)=>r.length?`<table><thead><tr>${h.map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>${r.join('')}</tbody></table>`:'<span class="muted">暂无</span>';
function render(){const d=snapshot,run=d.run;document.getElementById('subtitle').textContent=`${run.graph_id} · ${run.run_id} · 自动刷新`;document.getElementById('metrics').innerHTML=[['状态',run.phase],['代数',run.generation],['Token',run.tokens_used],['费用','$'+Number(run.cost_usd||0).toFixed(4)],['事件',run.event_cursor]].map(x=>`<div class="metric"><span class="muted">${esc(x[0])}</span><b>${esc(x[1])}</b></div>`).join('');document.getElementById('nodes').innerHTML=d.nodes.map(n=>`<div class="node ${esc(n.status)}"><b>${esc(n.id)}</b> <span class="pill">${esc(n.status)}</span><br><small>${esc(n.kind)} · 依赖 ${n.deps.length?n.deps.map(esc).join(', '):'无'} · 尝试 ${esc(n.attempts)} · $${Number(n.cost_usd||0).toFixed(4)}</small></div>`).join('');document.getElementById('artifacts').innerHTML=table(['Artifact','生产者','消费者'],d.artifacts.map(a=>`<tr><td>${esc(a.key)} v${esc(a.version)}</td><td>${esc(a.producer)}</td><td>${a.consumers.length?a.consumers.map(esc).join(', '):'—'}</td></tr>`));document.getElementById('approvals').innerHTML=table(['Gate','节点','状态','操作者','动作'],d.approvals.map(a=>`<tr><td>${esc(a.gate)}</td><td>${esc(a.node_id)}</td><td>${esc(a.status)}</td><td><input class="actor" data-gate="${esc(a.gate)}" placeholder="你的名字"><input class="note" data-gate="${esc(a.gate)}" placeholder="备注（可选）"></td><td><button data-gate="${esc(a.gate)}" data-decision="allow">批准</button><button class="deny" data-gate="${esc(a.gate)}" data-decision="deny">拒绝</button></td></tr>`));const sf=d.agent_os&&d.agent_os.reuse&&d.agent_os.reuse.singleflight,rs=d.agent_os&&d.agent_os.reuse;document.getElementById('reuse').innerHTML=rs?`<p><span class="pill ok">有效复用 ${esc(rs.entries.valid)}</span></p><p>累计节省 ${esc(rs.saved_tokens)} Token / ${rs.saved_cost_complete?'$'+Number(rs.saved_cost_usd).toFixed(4):'费用 unknown'}</p><p class="muted">运行 ${esc(sf.running)} · 完成 ${esc(sf.completed)} · 失败 ${esc(sf.failed)} · 异常 ${esc(sf.invalid)}</p>`:'<span class="muted">未连接 Agent OS 状态</span>';document.getElementById('publications').innerHTML=table(['来源','验证器','状态','质量/原因'],d.publications.map(p=>`<tr><td>${esc(p.source_node_id)}</td><td>${esc(p.verifier_id)}</td><td>${esc(p.status)}</td><td>${p.quality_score===null||p.quality_score===undefined?esc(p.reason):esc(p.quality_score)}</td></tr>`));document.getElementById('events').innerHTML=recent.length?recent.slice(-12).reverse().map(e=>`<div class="node"><b>${esc(e.event)}</b> <span class="muted">${esc(e.node_id)} · ${esc(e.time)}</span></div>`).join(''):'<span class="muted">暂无新增事件</span>'}
async function approve(gate,decision){const actor=document.querySelector(`.actor[data-gate="${CSS.escape(gate)}"]`).value.trim(),note=document.querySelector(`.note[data-gate="${CSS.escape(gate)}"]`).value.trim();if(!actor){notice('请先填写操作者',true);return}try{const r=await fetch('/api/approval',{method:'POST',headers:{'Content-Type':'application/json','X-Grapheng-Action-Token':actionToken},body:JSON.stringify({gate,decision,actor,note:note||null})});const v=await r.json();if(!r.ok)throw new Error(v.error||'审批失败');revision=v.update.revision;cursor=v.update.next_cursor;snapshot=v.update.snapshot;recent=(recent.concat(v.update.events||[])).slice(-50);render();notice('审批已写入统一收件箱；控制器恢复运行后生效')}catch(e){notice(e.message,true)}}
function notice(message,bad=false){const n=document.getElementById('notice');n.className=bad?'error':'flash';n.textContent=message;setTimeout(()=>{n.textContent=''},4000)}
document.getElementById('approvals').addEventListener('click',e=>{const b=e.target.closest('button[data-decision]');if(b)approve(b.dataset.gate,b.dataset.decision)});
async function poll(){try{const u=new URL('/api/snapshot',location.href);u.searchParams.set('revision',revision);u.searchParams.set('after',cursor);const r=await fetch(u);const v=await r.json();if(!r.ok)throw new Error(v.error||'刷新失败');revision=v.revision;cursor=v.next_cursor;recent=(recent.concat(v.events||[])).slice(-50);if(v.changed&&v.snapshot)snapshot=v.snapshot;render()}catch(e){notice(e.message,true)}finally{setTimeout(poll,1000)}}render();setTimeout(poll,1000);
</script></body></html>'''
