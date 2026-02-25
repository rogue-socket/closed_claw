# Purpose: Interactive terminal menu flows for task execution and setup.

from __future__ import annotations

import json
from argparse import Namespace


def run_main_menu(handlers: dict[str, callable]) -> int:
    """Run run main menu."""
    _print_launch_art()
    while True:
        print("\nClosed Claw")
        print("1) Setup (provider/model/API key)")
        print("2) Init")
        print("3) Doctor")
        print("4) Run Task")
        print("5) List Agents")
        print("6) View Agent Details")
        print("7) List Runs")
        print("8) List Audit")
        print("9) Show Run Log")
        print("10) List Tools")
        print("11) Delete Agent")
        print("12) Delete All Agents")
        print("13) Stop Run Loop (graceful)")
        print("14) Web Dashboard")
        print("15) Exit")

        choice = _safe_input("Choose an option: ")
        if choice is None:
            print("No interactive input available. Exiting menu.")
            return 0
        choice = choice.strip()
        if choice == "1":
            handlers["setup"](Namespace())
        elif choice == "2":
            handlers["init"](Namespace())
        elif choice == "3":
            handlers["doctor"](Namespace())
        elif choice == "4":
            _run_task_interactive(handlers)
        elif choice == "5":
            limit = _int_input("Limit [20]: ", default=20)
            handlers["agents"](Namespace(limit=limit))
        elif choice == "6":
            _agent_details_interactive(handlers)
        elif choice == "7":
            limit = _int_input("Limit [20]: ", default=20)
            handlers["runs"](Namespace(limit=limit))
        elif choice == "8":
            limit = _int_input("Limit [20]: ", default=20)
            handlers["audit"](Namespace(limit=limit))
        elif choice == "9":
            raw_run_id = _safe_input("Run ID: ")
            if raw_run_id is None:
                print("No interactive input available. Exiting menu.")
                return 0
            run_id = raw_run_id.strip()
            tail = _int_input("Tail [100]: ", default=100)
            if run_id:
                handlers["runlog"](Namespace(run_id=run_id, tail=tail))
            else:
                print("Run ID is required.")
        elif choice == "10":
            _tools_interactive(handlers)
        elif choice == "11":
            _delete_agent_interactive(handlers)
        elif choice == "12":
            _delete_all_agents_interactive(handlers)
        elif choice == "13":
            _cancel_run_interactive(handlers)
        elif choice == "14":
            handlers["web"](Namespace(host="127.0.0.1", port=7860))
        elif choice == "15":
            print("Goodbye.")
            return 0
        else:
            print("Invalid choice. Try again.")


def _run_task_interactive(handlers: dict[str, callable]) -> None:
    """Run run task interactive."""
    raw_task = _safe_input("Task: ")
    if raw_task is None:
        print("No interactive input available.")
        return
    task = raw_task.strip()
    if not task:
        print("Task is required.")
        return
    raw_session = _safe_input("Session ID [auto]: ")
    if raw_session is None:
        print("No interactive input available.")
        return
    session_id = raw_session.strip() or None
    raw_context = _safe_input("Context JSON string or file path [none]: ")
    if raw_context is None:
        print("No interactive input available.")
        return
    context_raw = raw_context.strip() or None
    create_mode = _choose_mode("Create approval mode", default="interactive")
    api_mode = _choose_mode("API approval mode", default="interactive")
    llm_provider = _choose_provider(default="heuristic")
    llm_model = input("LLM model [provider default]: ").strip() or None
    organize_path = _safe_input("Organize path override [none]: ")
    dry_run = (_safe_input("Organize dry-run? (yes/no) [no]: ") or "").strip().lower() in {"yes", "y"}
    recursive = (_safe_input("Organize recursive? (yes/no) [no]: ") or "").strip().lower() in {"yes", "y"}

    handlers["run"](
        Namespace(
            task=task,
            session_id=session_id,
            context_json=context_raw,
            create_approval_mode=create_mode,
            api_approval_mode=api_mode,
            llm_provider=llm_provider,
            llm_model=llm_model,
            organize_path=(organize_path or "").strip() or None,
            organize_dry_run=dry_run,
            organize_recursive=recursive,
        )
    )


def _delete_agent_interactive(handlers: dict[str, callable]) -> None:
    """Run delete agent interactive."""
    raw = _safe_input("Agent ID to delete: ")
    if raw is None:
        print("No interactive input available.")
        return
    agent_id = raw.strip()
    if not agent_id:
        print("Agent ID is required.")
        return
    confirm = _safe_input(f"Type 'yes' to permanently delete agent '{agent_id}': ")
    if (confirm or "").strip().lower() not in {"yes", "y"}:
        print("Cancelled.")
        return
    handlers["delete_agent"](Namespace(agent_id=agent_id, yes=True))


def _delete_all_agents_interactive(handlers: dict[str, callable]) -> None:
    """Run delete all agents interactive."""
    confirm = _safe_input("Type 'DELETE ALL' to permanently remove all agents: ")
    if (confirm or "").strip() != "DELETE ALL":
        print("Cancelled.")
        return
    handlers["delete_all_agents"](Namespace(yes=True))


def _cancel_run_interactive(handlers: dict[str, callable]) -> None:
    """Run cancel run interactive."""
    raw = _safe_input("Run ID to gracefully stop: ")
    run_id = (raw or "").strip()
    if not run_id:
        print("Run ID is required.")
        return
    handlers["cancel_run"](Namespace(run_id=run_id))


def _tools_interactive(handlers: dict[str, callable]) -> None:
    """Run tools interactive."""
    raw = _safe_input("Agent ID [blank for global tools]: ")
    agent_id = (raw or "").strip() or None
    handlers["tools"](Namespace(agent_id=agent_id))


def _agent_details_interactive(handlers: dict[str, callable]) -> None:
    """Run agent details interactive."""
    raw = _safe_input("Agent ID: ")
    agent_id = (raw or "").strip()
    if not agent_id:
        print("Agent ID is required.")
        return
    handlers["agent"](Namespace(agent_id=agent_id))


def _choose_mode(label: str, default: str) -> str:
    """Run choose mode."""
    options = ["interactive", "approve", "deny"]
    print(f"{label}:")
    for i, mode in enumerate(options, start=1):
        print(f"  {i}. {mode}")
    raw_in = _safe_input(f"Choice [{default}]: ")
    if raw_in is None:
        return default
    raw = raw_in.strip()
    if not raw:
        return default
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return options[int(raw) - 1]
    if raw in options:
        return raw
    print(f"Invalid mode. Using {default}.")
    return default


def _choose_provider(default: str) -> str:
    """Run choose provider."""
    options = ["heuristic", "openai", "gemini", "claude"]
    print("LLM provider:")
    for i, provider in enumerate(options, start=1):
        print(f"  {i}. {provider}")
    raw_in = _safe_input(f"Choice [{default}]: ")
    if raw_in is None:
        return default
    raw = raw_in.strip()
    if not raw:
        return default
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return options[int(raw) - 1]
    if raw in options:
        return raw
    print(f"Invalid provider. Using {default}.")
    return default


def _int_input(prompt: str, default: int) -> int:
    """Run int input."""
    raw_in = _safe_input(prompt)
    if raw_in is None:
        return default
    raw = raw_in.strip()
    if not raw:
        return default
    if raw.isdigit():
        return int(raw)
    print(f"Invalid number. Using {default}.")
    return default


def _safe_input(prompt: str) -> str | None:
    """Run safe input."""
    try:
        return input(prompt)
    except EOFError:
        return None


def _print_launch_art() -> None:
    """Run print launch art."""
    print(
        r"""
   ______ _                    _   _____ _
  / ____| |                  | | / ____| |
 | |    | | ___  ___  ___  __| || |    | | __ ___      __
 | |    | |/ _ \/ __|/ _ \/ _` || |    | |/ _` \ \ /\ / /
 | |____| | (_) \__ \  __/ (_| || |____| | (_| |\ V  V /
  \_____|_|\___/|___/\___|\__,_| \_____|_|\__,_| \_/\_/
        """
    )
