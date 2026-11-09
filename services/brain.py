"""Small orchestration primitives extending Rukiya's existing services.

Local skills provide exact operations; free-form conversation still needs a model.
"""
from __future__ import annotations

import ast
import asyncio
import operator
import math
import re
import time
from collections import Counter, OrderedDict, deque
from contextlib import asynccontextmanager
from threading import Lock


class BrainBusy(RuntimeError):
    pass


class BrainRuntime:
    """No unbounded queue or expiring active locks. State belongs to one event loop."""
    def __init__(self, config):
        self.config = config
        self.active = {}
        self.users = Counter()
        self.guilds = Counter()
        self.closed = False
        self._metrics = deque(maxlen=200)
        self._metric_lock = Lock()

    @asynccontextmanager
    async def admit(self, message):
        from services.scopes import conversation_scope
        owner = (message.platform, message.user_id or message.username, message.guild_id)
        bucket = (message.platform, message.guild_id or message.video_id or message.channel_id or owner[1])
        key = (owner, conversation_scope(message))
        if (self.closed or key in self.active or len(self.active) >= self.config.brain_max_active
                or self.users[owner] >= self.config.brain_user_active
                or self.guilds[bucket] >= self.config.brain_guild_active):
            raise BrainBusy()
        self.active[key] = asyncio.current_task()
        self.users[owner] += 1
        self.guilds[bucket] += 1
        try:
            yield
        finally:
            self.active.pop(key, None)
            for counts, item in ((self.users, owner), (self.guilds, bucket)):
                counts[item] -= 1
                if counts[item] <= 0:
                    counts.pop(item, None)

    def observe(self, started, outcome):
        with self._metric_lock:
            self._metrics.append(((time.perf_counter() - started) * 1000, outcome))

    def snapshot(self):
        with self._metric_lock:
            rows = list(self._metrics)
        times = sorted(row[0] for row in rows)
        return {"active": len(self.active), "samples": len(rows),
                "p95_ms": round(times[max(0, int(len(times) * .95) - 1)], 3) if times else None,
                "outcomes": dict(Counter(row[1] for row in rows))}

    async def cancel(self, platform, user_id, guild_id):
        tasks = [task for (owner, _), task in self.active.items()
                 if owner == (platform, user_id, guild_id) and task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self):
        self.closed = True
        tasks = set(self.active.values()) - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def calculate(expression):
    """Arithmetic only; caps prevent large exponentiation or expression trees."""
    if len(expression) > 180:
        raise ValueError("expression too long")
    tree = ast.parse(expression, mode="eval")
    if len(list(ast.walk(tree))) > 50:
        raise ValueError("expression too complex")
    operations = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
                  ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
                  ast.Mod: operator.mod, ast.Pow: operator.pow}

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            value = node.value
        elif isinstance(node, ast.UnaryOp) and type(node.op) in (ast.UAdd, ast.USub):
            value = visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        elif isinstance(node, ast.BinOp) and type(node.op) in operations:
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 12:
                raise ValueError("exponent too large")
            value = operations[type(node.op)](left, right)
        else:
            raise ValueError("only arithmetic is supported")
        if not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 1e15:
            raise ValueError("result out of range")
        return value
    return f"{visit(tree):g}"


class LocalSkills:
    def __init__(self):
        self.cache = OrderedDict()
        self.hits = self.misses = 0

    def respond(self, request):
        text = re.sub(r"^(?:hey\s+)?(?:rukiya|rukia|ruki)[,:]?\s+", "", request.text.strip(), flags=re.I)
        match = re.fullmatch(r"(?:calc|calculate)\s+(.+)", text, flags=re.I)
        expression = match.group(1) if match else text if re.fullmatch(r"[\d\s.()+*/%\-]+", text) else None
        if expression is not None:
            cached = self.cache.get(expression)
            if cached and cached[1] > time.monotonic():
                self.hits += 1
                self.cache.move_to_end(expression)
                return cached[0]
            self.misses += 1
            try:
                answer = calculate(expression)
            except (ValueError, SyntaxError, ArithmeticError, TypeError):
                return "That calculation is invalid or exceeds the supported limits."
            self.cache[expression] = (answer, time.monotonic() + 300)
            while len(self.cache) > 256:
                self.cache.popitem(last=False)
            return answer
        if text.casefold().rstrip('?') in {"my id", "my user id"} and request.user_id:
            return str(request.user_id)
        match = re.fullmatch(r"memory:\s*([\w -]{1,64})", text, re.I)
        if match:
            key = match.group(1).strip().casefold()
            for record in request.persistent_memory or []:
                name = record.get('key') if isinstance(record, dict) else record.key
                if name.casefold() == key:
                    value = record.get('value') if isinstance(record, dict) else record.value
                    return str(value)[:220]
            return "No approved memory matches that key in this scope."
        return None


STYLES = {
    "adaptive": "Match the current viewer's tone; preserve the character brief without forcing teasing.",
    "casual": "Use natural, short chat phrasing without repeated greetings.",
    "friendly": "Be quietly warm and direct, without flattery.",
    "playful": "Use understated humor only when the conversation invites it.",
    "sarcastic": "Use mild invited sarcasm, never cruelty or sensitive-topic teasing.",
    "serious": "Be calm and direct; omit jokes and sarcasm.",
    "technical": "Prioritize exact facts and usable steps over character flourishes.",
    "helpful": "Answer first, then give one useful reason or next step.",
    "concise": "Use the shortest useful answer and omit filler.",
}


def response_style(request):
    if request.intent in {"crisis", "sensitive"}:
        return STYLES['serious']
    if request.style in STYLES and request.style != 'adaptive':
        return STYLES[request.style]
    if re.search(r"\b(code|python|javascript|sql|debug|asyncio)\b", request.text, re.I):
        return STYLES['technical']
    return STYLES['adaptive']
