"""
MiroFish Price Forecast Pipeline
Automates: seed.md → Ontology → Graph → Simulation → Interview → price_forecast.csv
"""

import os
import sys
import csv
import json
import time
import re
import hashlib
import argparse
import fcntl
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional, Tuple

import requests

from common import project_root, resolve_path, LLMClient


# ============== Helpers ==============

def _load_graph_cache(sidecar_path: str, sha256: str) -> Optional[Tuple[str, str, str]]:
    """Return (project_id, graph_id, simulation_id) from sidecar if sha256 key exists, else None.

    Returns None if the entry is incomplete (missing any required field).
    Backward compatible with old sidecars that only have project_id + graph_id.
    """
    if not os.path.exists(sidecar_path):
        return None
    try:
        with open(sidecar_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        entry = data.get(sha256)
        if entry:
            project_id = entry.get("project_id")
            graph_id = entry.get("graph_id")
            simulation_id = entry.get("simulation_id")
            # Only return if all three fields present
            if project_id and graph_id and simulation_id:
                return project_id, graph_id, simulation_id
    except json.JSONDecodeError:
        pass
    return None


def _save_graph_cache(sidecar_path: str, sha256: str, project_id: str, graph_id: str, simulation_id: str) -> None:
    """Add or update sha256 entry in the sidecar dict with all three IDs, preserving all other entries."""
    data = {}
    if os.path.exists(sidecar_path):
        try:
            with open(sidecar_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}
    data[sha256] = {
        "project_id": project_id,
        "graph_id": graph_id,
        "simulation_id": simulation_id,
        "cached_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        with open(sidecar_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass  # cache write failure is non-fatal


def poll(check_fn, interval=3, max_wait=600):
    """Poll check_fn() until it returns a truthy result or timeout."""
    start = time.time()
    while time.time() - start < max_wait:
        result = check_fn()
        if result:
            return result
        time.sleep(interval)
    raise TimeoutError(f"Polling timed out after {max_wait}s")


def api(method, base_url, path, session=None, **kwargs):
    """Make an API call and return parsed response. Raises on failure."""
    http = session or requests
    url = f"{base_url}{path}"
    resp = getattr(http, method)(url, **kwargs)
    data = resp.json()
    if not data.get("success"):
        error = data.get("error") or data.get("message") or str(data)
        raise RuntimeError(f"API error ({path}): {error}")
    return data.get("data", {})


def count_agents_in_seed(seed_text):
    """Count agent entries in '# Agent Population' section in seed.md."""
    section_title = "# Agent Population"
    if section_title in seed_text:
        agents_section = seed_text.split(section_title, 1)[1]
        try:
            import json
            bracket_start = agents_section.find("[")
            bracket_end = agents_section.find("]")
            if bracket_start != -1 and bracket_end != -1:
                agents_json = json.loads(agents_section[bracket_start:bracket_end+1])
                return len(agents_json)
        except (json.JSONDecodeError, ValueError):
            pass
    return 6


def strip_agents_section(seed_text):
    """Remove the '# Agents Population' section and everything after it."""
    if "# Agents Population" in seed_text:
        return seed_text.split("# Agents Population", 1)[0]
    return seed_text


def extract_latest_timestamp(seed_text):
    """Return Unix seconds of the latest OHLCV candle in seed_text, or now() as fallback.

    The seed's 1H table lists rows newest-first in the format:
        2026-04-06 23:00 |  68,777.00 | ...
    We find the first data row after the '## 1H' header.
    """
    match = re.search(
        r'## 1H\n'
        r'Date/Time[^\n]*\n'
        r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2})',
        seed_text
    )
    if match:
        dt = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return int(time.time())


def extract_timestamp_from_seed_path(seed_path):
    """Extract Unix seconds from seed filename.

    Supports:
      seed_YYYY-MM-DDTHH.md
      YYYY-MM-DD-HH-MM.md
    Returns None if no pattern matches.
    """
    base = os.path.basename(seed_path)
    m = re.search(r"seed_(\d{4}-\d{2}-\d{2}T\d{2})\.md$", base)
    if m:
        dt = datetime.strptime(m.group(1), "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    m = re.search(r"(\d{4}-\d{2}-\d{2})-(\d{2})-(\d{2})", base)
    if m:
        dt = datetime.strptime(f"{m.group(1)} {m.group(2)}:{m.group(3)}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return None


def _format_chart_time(ts):
    """Format Unix seconds as UTC hour timestamp (YYYY-MM-DD HH:MM)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


# ============== Pipeline Steps ==============

def check_server(base_url, session=None):
    """Check if Flask server is reachable."""
    http = session or requests
    try:
        http.get(f"{base_url}/api/graph/tasks", timeout=5)
        return True
    except requests.ConnectionError:
        return False


def step1_generate_ontology(base_url, seed_path, requirement, session=None):
    """Upload seed file and generate ontology."""
    print("[Step 1/5] Generating ontology...", end="", flush=True)

    filename = os.path.basename(seed_path)
    with open(seed_path, "rb") as f:
        files = {"files": (filename, f)}
        form_data = {"simulation_requirement": requirement}
        result = api("post", base_url, "/api/graph/ontology/generate",
                     session=session, files=files, data=form_data)

    project_id = result["project_id"]
    entity_count = len(result.get("ontology", {}).get("entity_types", []))
    print(f"  ✓ {project_id} ({entity_count} entity types)")
    return project_id


def step2_build_graph(base_url, project_id, session=None):
    """Build Zep knowledge graph and wait for completion."""
    print("[Step 2/5] Building knowledge graph...", end="", flush=True)
    t0 = time.time()

    result = api("post", base_url, "/api/graph/build",
                 session=session, json={"project_id": project_id})
    task_id = result["task_id"]

    def check():
        task = api("get", base_url, f"/api/graph/task/{task_id}", session=session)
        status = task.get("status")
        if status == "completed":
            return task
        if status == "failed":
            raise RuntimeError(f"Graph build failed: {task.get('error')}")
        return None

    task = poll(check, interval=1, max_wait=300)
    graph_id = task.get("result", {}).get("graph_id")
    elapsed = int(time.time() - t0)
    print(f"  ✓ {graph_id} ({elapsed}s)")
    return graph_id


def step3_prepare_simulation(base_url, project_id, graph_id, agent_count=20, session=None):
    """Create simulation and prepare (profiles + config)."""
    print("[Step 3/5] Preparing simulation...", end="", flush=True)
    t0 = time.time()

    # Create
    result = api("post", base_url, "/api/simulation/create",
                 session=session, json={"project_id": project_id, "graph_id": graph_id})
    simulation_id = result["simulation_id"]

    # Prepare — parallel_profile_count = agent_count so all profiles generate in one batch
    result = api("post", base_url, "/api/simulation/prepare",
                 session=session, json={"simulation_id": simulation_id, "parallel_profile_count": agent_count})

    if result.get("already_prepared"):
        agents_count = result.get("prepare_info", {}).get("profiles_count", "?")
        elapsed = int(time.time() - t0)
        print(f"  ✓ {simulation_id} | {agents_count} agents (already prepared, {elapsed}s)")
        return simulation_id

    task_id = result.get("task_id")

    def check():
        status_data = api("post", base_url, "/api/simulation/prepare/status",
                          session=session, json={"task_id": task_id, "simulation_id": simulation_id})
        if status_data.get("already_prepared"):
            return status_data
        status = status_data.get("status")
        if status == "completed":
            return status_data
        if status == "failed":
            raise RuntimeError(f"Preparation failed: {status_data.get('error')}")
        progress = status_data.get("progress", 0)
        print(f"\r[Step 3/5] Preparing simulation... {progress}%", end="", flush=True)
        return None

    status_data = poll(check, interval=5, max_wait=600)
    elapsed = int(time.time() - t0)
    agents_count = status_data.get("prepare_info", {}).get("profiles_count",
                   status_data.get("result", {}).get("agents_count", "?"))
    print(f"\r[Step 3/5] Preparing simulation...  ✓ {simulation_id} | {agents_count} agents ({elapsed}s)")
    return simulation_id


def step4_run_simulation(base_url, simulation_id, max_rounds=5, session=None):
    """Start OASIS simulation and wait for completion."""
    print("[Step 4/5] Running simulation...", end="", flush=True)
    t0 = time.time()

    api("post", base_url, "/api/simulation/start",
        session=session, json={"simulation_id": simulation_id, "platform": "parallel", "max_rounds": max_rounds})

    def check():
        status = api("get", base_url, f"/api/simulation/{simulation_id}/run-status", session=session)
        runner = status.get("runner_status", "idle")
        if runner == "completed":
            return status
        if runner in ("failed", "stopped"):
            raise RuntimeError(f"Simulation {runner}: {status.get('error', '')}")
        current = status.get("current_round", 0)
        total = status.get("total_rounds", "?")
        actions = status.get("total_actions_count", 0)
        print(f"\r[Step 4/5] Running simulation... round {current}/{total}, {actions} actions",
              end="", flush=True)
        return None

    max_wait = max(600, max_rounds * 60)
    status = poll(check, interval=2, max_wait=max_wait)
    elapsed = int(time.time() - t0)
    total_rounds = status.get("total_rounds", "?")
    total_actions = status.get("total_actions_count", 0)
    print(f"\r[Step 4/5] Running simulation...  ✓ {total_rounds} rounds, {total_actions} actions ({elapsed}s)")
    return {
        "simulation_id": simulation_id,
        "total_rounds": total_rounds,
        "total_actions": total_actions,
    }


# ============== Interview & Decision Parsing ==============

def _parse_range(response_text):
    """Parse two floats from agent response. Returns (float, float) or None.

    Uses regex extraction so the response can contain extra words/punctuation
    (e.g. 'The range is 83000.5,85200.0' or '83000.5 to 85200.0').
    Validates: both values > 0, low < high.
    """
    if not isinstance(response_text, str):
        return None
    nums = re.findall(r'\d+(?:\.\d+)?', response_text)
    if len(nums) < 2:
        return None
    try:
        low, high = float(nums[0]), float(nums[1])
    except ValueError:
        return None
    if low <= 0 or high <= 0 or low >= high:
        return None
    return (low, high)


def _fmt_forecast(agent_name, range_low, range_high):
    """Format a price forecast as a display string."""
    return f"{agent_name}: {range_low} — {range_high}"


def _average_forecasts(forecasts):
    """Aggregate per-agent forecasts into one consensus average forecast."""
    if not forecasts:
        return []

    lows = [float(item["range_low"]) for item in forecasts]
    highs = [float(item["range_high"]) for item in forecasts]
    avg_low = sum(lows) / len(lows)
    avg_high = sum(highs) / len(highs)

    if avg_low >= avg_high:
        print(f"Warning: degenerate consensus range (avg_low={avg_low}, avg_high={avg_high}), adjusting")
        avg_high = avg_low + 1e-6

    return [{
        "name": "consensus_avg",
        "range_low": avg_low,
        "range_high": avg_high,
    }]


def _summary_prediction(forecasts):
    """Return one predicted low/high pair from forecast list using simple average."""
    if not forecasts:
        return None
    avg = _average_forecasts(forecasts)[0]
    return avg["range_low"], avg["range_high"]


def _forecast_from_persona(llm, agent_name, persona, world_seed, predict_hours):
    """Generate price range forecast directly from agent persona + market data (single LLM call).
    llm.chat() returns a plain string — we parse it directly with _parse_range.
    """
    response = llm.chat(
        messages=[
            {
                "role": "system",
                "content": (
                    f"You are {agent_name}, a trader with the following profile:\n{persona}\n\n"
                    "Based on your personality and trading style, predict the price range.\n\n"
                    "**CRITICAL: You MUST respond with EXACTLY two numbers separated by a comma. "
                    "Nothing else. No words, no explanation, no punctuation other than the comma "
                    "and decimal point.**\n"
                    "Format: range_low,range_high"
                )
            },
            {
                "role": "user",
                "content": (
                    f"Market data:\n{world_seed}\n\n"
                    f"What is the price range for the next {predict_hours} hours? "
                    "Respond with ONLY two numbers separated by a comma."
                )
            }
        ],
        temperature=0.8
    )
    return _parse_range(response)


def _get_agent_profiles(base_url, simulation_id, session=None):
    """Fetch agent profiles, trying twitter then reddit. Returns (id_to_profile, platform)."""
    for platform in ("twitter", "reddit"):
        profiles_data = api("get", base_url,
                            f"/api/simulation/{simulation_id}/profiles?platform={platform}",
                            session=session)
        profiles = profiles_data.get("profiles", [])
        if profiles:
            id_to_profile = {}
            for p in profiles:
                agent_id = p.get("user_id", p.get("agent_id"))
                if agent_id is not None:
                    id_to_profile[int(agent_id)] = p
            return id_to_profile, platform

    raise RuntimeError("No agent profiles found")


def _interview_agents(base_url, simulation_id, seed_text, predict_hours, id_to_profile, platform, session=None):
    """Interview agents via OASIS for price range forecasts (parallel)."""
    world_seed = strip_agents_section(seed_text)
    agent_count = len(id_to_profile)

    interview_prompt = (
        "Here is the current market context:\n"
        f"{world_seed}\n\n"
        f"Based on this data and your discussions, predict the price range for the next {predict_hours} hours.\n\n"
        "**CRITICAL: You MUST respond with EXACTLY two numbers separated by a comma. "
        "Nothing else. No words, no explanation, no punctuation other than the comma and decimal point.**\n"
        "Format: range_low,range_high"
    )

    interview_timeout = max(60, agent_count * 15)
    interview_result = api("post", base_url, "/api/simulation/interview/all",
                           session=session, json={
                               "simulation_id": simulation_id,
                               "prompt": interview_prompt,
                               "platform": platform,
                               "timeout": interview_timeout
                           })

    raw_results = interview_result.get("result", {}).get("results", {})

    parse_tasks = []
    for idx, (key, interview) in enumerate(raw_results.items(), 1):
        agent_id = interview.get("agent_id")
        response_text = interview.get("response", "")
        profile = id_to_profile.get(agent_id, {})
        agent_name = profile.get("name", profile.get("user_name", f"agent_{agent_id}"))

        if not response_text:
            print(f"  [{idx}/{agent_count}] {agent_name}: (no response)")
            continue

        parse_tasks.append((idx, agent_name, response_text))

    forecasts = []
    for idx, name, resp in parse_tasks:
        result = _parse_range(resp)
        if result:
            low, high = result
            forecasts.append({"name": name, "range_low": low, "range_high": high})
            print(f"  [{idx}/{agent_count}] {_fmt_forecast(name, low, high)}")
        else:
            print(f"  [{idx}/{agent_count}] {name}: could not parse forecast")

    return forecasts


def _fallback_persona_decisions(llm, seed_text, id_to_profile, predict_hours):
    """Generate price range forecasts from agent personas when interview is unavailable (parallel)."""
    world_seed = strip_agents_section(seed_text)
    agent_count = len(id_to_profile)

    print(f"  Falling back to persona-based forecasts for {agent_count} agents")

    tasks = []
    for idx, (agent_id, profile) in enumerate(id_to_profile.items(), 1):
        agent_name = profile.get("name", profile.get("user_name", f"agent_{agent_id}"))
        persona = profile.get("persona", profile.get("bio", ""))
        tasks.append((idx, agent_name, persona))

    forecasts = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(_forecast_from_persona, llm, name, persona, world_seed, predict_hours): (idx, name)
            for idx, name, persona in tasks
        }
        for future in as_completed(futures):
            idx, name = futures[future]
            try:
                result = future.result()
                if result:
                    low, high = result
                    forecasts.append({"name": name, "range_low": low, "range_high": high})
                    print(f"  [{idx}/{agent_count}] {_fmt_forecast(name, low, high)}")
                else:
                    print(f"  [{idx}/{agent_count}] {name}: could not parse forecast")
            except Exception as e:
                print(f"  [{idx}/{agent_count}] {name}: error: {e}")

    return forecasts


def step5_interview_for_trades(base_url, simulation_id, llm, seed_text, predict_hours, session=None):
    """Interview each agent for price range forecast, return list of forecast dicts."""
    print("[Step 5/5] Interviewing agents for price forecasts...")

    id_to_profile, platform = _get_agent_profiles(base_url, simulation_id, session=session)
    agent_count = len(id_to_profile)

    run_status = api("get", base_url, f"/api/simulation/{simulation_id}/run-status", session=session)
    runner_status = run_status.get("runner_status")
    env_alive = runner_status not in ("stopped", "failed", "idle")

    # Also attempt interview when runner shows "completed" — the env may still be alive
    # in command-wait mode. We check env_alive status below to confirm before sending.
    if env_alive or runner_status == "completed":
        print(f"  Found {agent_count} agents (platform: {platform}) — trying OASIS interview...")
        # Wait for the environment to enter command-wait mode (env_status.json → "alive").
        # The monitor thread sets runner_status="completed" from action logs *before* the
        # simulation process calls ipc_handler.update_status("alive"), creating a race window.
        env_status_alive = False
        for _ in range(60):  # up to 60 seconds
            env_check = api("post", base_url, "/api/simulation/env-status",
                            session=session, json={"simulation_id": simulation_id})
            if env_check.get("env_alive"):
                env_status_alive = True
                break
            time.sleep(1)
        if not env_status_alive:
            print(f"  Environment not ready for interview (timed out waiting for alive status)")
        else:
            try:
                forecasts = _interview_agents(base_url, simulation_id, seed_text, predict_hours,
                                              id_to_profile, platform, session=session)
                if forecasts:
                    return forecasts
            except Exception as e:
                print(f"  Interview failed: {e}")

    return _fallback_persona_decisions(llm, seed_text, id_to_profile, predict_hours)


# ============== CSV Output ==============

def write_csv(output_path, latest_chart_time, predicted_low, predicted_high,
              actual_low, actual_high, agent_count, simulation_rounds, runtime, prev_mid=None):
    """Write one summary row with prediction + metadata fields.

    If the file already exists, append a new row; otherwise create with header.
    Uses file locking to prevent race conditions when multiple processes write simultaneously.
    """
    fieldnames = [
        "latest_chart_time",
        "predicted_low",
        "predicted_high",
        "actual_low",
        "actual_high",
        "prev_mid",
        "agent_count",
        "simulation_rounds",
        "runtime",
    ]

    with open(output_path, "a", newline="", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)  # Exclusive lock: wait until lock is available
        try:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if f.tell() == 0:  # Check inside lock to avoid race between processes
                writer.writeheader()
            writer.writerow({
                "latest_chart_time": latest_chart_time,
                "predicted_low": predicted_low,
                "predicted_high": predicted_high,
                "actual_low": actual_low,
                "actual_high": actual_high,
                "prev_mid": prev_mid if prev_mid is not None else "",
                "agent_count": agent_count,
                "simulation_rounds": simulation_rounds,
                "runtime": runtime,
            })
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)  # Release lock


# ============== Main ==============

def main():
    pipeline_start = time.time()

    parser = argparse.ArgumentParser(description="MiroFish Price Forecast Pipeline")
    parser.add_argument("seed_file", help="Path to seed file (md/txt)")
    default_output = os.path.join(project_root, '..', 'rust-connectors', 'mm-simulation', 'data', 'actions.csv')
    parser.add_argument("-o", "--output", default=default_output,
                        help="Output CSV path (default: ../rust-connectors/mm-simulation/data/actions.csv)")
    parser.add_argument("-r", "--requirement", default=None,
                        help="Simulation requirement (default: uses seed file content)")
    parser.add_argument("--base-url", default="http://localhost:5001",
                        help="Flask server URL (default: http://localhost:5001)")
    parser.add_argument("--rounds", type=int, default=5,
                        help="Max simulation rounds (default: 5)")
    parser.add_argument("--predict-hours", type=int, default=1,
                        help="Forecast horizon in hours (default: 1)")
    parser.add_argument("--aggregate", choices=["none", "average"], default="none",
                        help="Aggregate agent forecasts into one result (default: none)")
    parser.add_argument("--actual-low", type=float, default=None,
                        help="Optional actual next-candle low for backtest logging")
    parser.add_argument("--actual-high", type=float, default=None,
                        help="Optional actual next-candle high for backtest logging")
    parser.add_argument("--prev-mid", type=float, default=None,
                        help="Optional mid of the seed candle (for DA metric)")
    args = parser.parse_args()

    args.output = resolve_path(args.output)

    if not os.path.exists(args.seed_file):
        print(f"Error: seed file not found: {args.seed_file}")
        sys.exit(1)

    with open(args.seed_file, "r", encoding="utf-8") as f:
        seed_text = f.read()

    requirement = args.requirement or seed_text

    print("MiroFish Trade Pipeline")
    print("=" * 50)

    session = requests.Session()

    if not check_server(args.base_url, session=session):
        print(f"Error: Cannot connect to server at {args.base_url}")
        print("Start the server first: cd backend && python run.py")
        sys.exit(1)
    print(f"Server: {args.base_url} ✓")
    print()

    llm = LLMClient()

    # Graph-id cache — skip steps 1 & 2 if seed content unchanged
    seed_sha256 = hashlib.sha256(seed_text.encode("utf-8")).hexdigest()
    seed_dir = os.path.dirname(os.path.abspath(args.seed_file))
    seed_basename = os.path.basename(args.seed_file)
    cache_dir = os.path.join(seed_dir, ".cache")
    sidecar_path = os.path.join(cache_dir, seed_basename + ".json")

    agent_count = count_agents_in_seed(seed_text)

    cached = _load_graph_cache(sidecar_path, seed_sha256)
    if cached:
        project_id, graph_id, simulation_id = cached
        print(f"[Cache] Hit — skipping steps 1, 2, 3 (sha256: {seed_sha256[:12]}...)")
    else:
        project_id = step1_generate_ontology(args.base_url, args.seed_file, requirement, session=session)
        graph_id = step2_build_graph(args.base_url, project_id, session=session)
        simulation_id = step3_prepare_simulation(args.base_url, project_id, graph_id,
                                                 agent_count=agent_count, session=session)
        os.makedirs(cache_dir, exist_ok=True)
        _save_graph_cache(sidecar_path, seed_sha256, project_id, graph_id, simulation_id)
        print(f"[Cache] Saved (sha256: {seed_sha256[:12]}...)")
    sim_result = step4_run_simulation(args.base_url, simulation_id, max_rounds=args.rounds, session=session)
    forecasts = step5_interview_for_trades(args.base_url, simulation_id, llm, seed_text,
                                           args.predict_hours, session=session)

    source_agent_count = len(forecasts)

    if forecasts and args.aggregate == "average":
        forecasts = _average_forecasts(forecasts)
        avg = forecasts[0]
        print(f"Consensus average: {avg['range_low']} — {avg['range_high']}")

    if forecasts:
        summary = _summary_prediction(forecasts)
        if not summary:
            print("Warning: Could not compute summary prediction")
            sys.exit(1)

        predicted_low, predicted_high = summary
        latest_ts = extract_timestamp_from_seed_path(args.seed_file)
        if latest_ts is None:
            latest_ts = extract_latest_timestamp(seed_text)
        latest_chart_time = _format_chart_time(latest_ts)

        pipeline_elapsed = (time.time() - pipeline_start) / 60.0
        write_csv(
            output_path=args.output,
            latest_chart_time=latest_chart_time,
            predicted_low=predicted_low,
            predicted_high=predicted_high,
            actual_low=args.actual_low,
            actual_high=args.actual_high,
            prev_mid=args.prev_mid,
            agent_count=source_agent_count,
            simulation_rounds=sim_result.get("total_rounds", args.rounds),
            runtime=pipeline_elapsed,
        )
        print()
        print("=" * 50)
        print(f"Output: {args.output} (1 summary row)")
    else:
        print()
        print("=" * 50)
        print("Warning: No valid price forecasts were generated")
        sys.exit(1)


if __name__ == "__main__":
    main()
