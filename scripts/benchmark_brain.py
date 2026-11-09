"""Offline synthetic benchmark: python -m scripts.benchmark_brain.

No models, platform credentials, downloads or external requests are required.
"""
from __future__ import annotations

import asyncio
import json
import logging
import platform
import resource
import statistics
import tempfile
import time
from pathlib import Path

from services.config import Config
from services.ai_engine.engine import AIEngine
from services.ai_engine.models import AIEngineRequest
from services.ai_engine.planner import Planner
from services.memory_service import MemoryService
from services.identity_service import IdentityService
from services.models import ChatMessage
from services.orchestrator import RukiyaOrchestrator


def stats(values):
    return {'n':len(values),'median_ms':round(statistics.median(values),4),
            'p95_ms':round(sorted(values)[max(0,int(len(values)*.95)-1)],4)}


async def main():
    logging.disable(logging.CRITICAL)
    cfg=Config()
    cfg.external_ai_enabled = False
    cfg.local_ai_enabled = False
    cfg.memory_history_enabled = False
    cfg.memory_auto_extract = False
    planner=Planner(cfg)
    request=AIEngineRequest('why does Python asyncio help?','viewer')
    timings=[]
    for _ in range(1000):
        started=time.perf_counter();planner.plan(request)
        timings.append((time.perf_counter()-started)*1000)
    with tempfile.TemporaryDirectory() as tmp:
        cfg.db_path=str(Path(tmp)/'bench.db')
        memory=MemoryService(cfg)
        identity=IdentityService(memory)
        msg=ChatMessage('discord','0','123','Viewer','Viewer','calculate (25+5)*4',guild_id='1',channel_id='2')
        user=identity.resolve(msg)
        memory.set_user_memory(user.canonical_id,'favorite_game','Minecraft',source='user_approved')
        lookup=[]
        for _ in range(100):
            started=time.perf_counter();await memory.run_async(memory.get_relevant_user_memory,user,'favorite game')
            lookup.append((time.perf_counter()-started)*1000)
        for index in range(100):
            memory.add_brain_knowledge('discord:guild:1',f'Guide {index}',f'Python asyncio task queue guide number {index}')
        retrieval=[]
        for _ in range(50):
            started=time.perf_counter();await memory.run_async(memory.search_brain_knowledge,'discord:guild:1','Python asyncio task queue')
            retrieval.append((time.perf_counter()-started)*1000)
        engine=AIEngine(cfg)
        orch=RukiyaOrchestrator(config=cfg,memory_service=memory,identity_service=identity,ai_engine=engine)
        end_to_end=[];errors=0
        for index in range(100):
            msg.message_id=str(index)
            started=time.perf_counter()
            result=await orch.process_message(msg,bypass_trigger=True,bypass_cooldown=True)
            end_to_end.append((time.perf_counter()-started)*1000)
            errors += not result or result.text != '120'
        result={'python':platform.python_version(),'platform':platform.system(),
                'planning':stats(timings),'memory_lookup':stats(lookup),
                'retrieval_100_documents':stats(retrieval),'deterministic_full_pipeline':stats(end_to_end),
                'provider_operations':dict(engine.provider_operations),
                'deterministic_error_rate':errors/100,'deterministic_cache_hit_rate':engine.local_skills.hits/100,
                'fts5_available':memory.brain_fts,'process_peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                'first_token_ms':None,'real_model_latency_ms':None,
                'notes':'Sequential offline synthetic run; includes orchestrator identity, policy, SQLite and safety; no model loaded, no platform transports.'}
        await orch.aclose()
        print(json.dumps(result,indent=2))

if __name__=='__main__':
    asyncio.run(main())
