#!/usr/bin/env python3
"""CLI entrypoint: ask the policy assistant a question from the command line."""

import argparse
import json

from app.graph import ask


def main():
    parser = argparse.ArgumentParser(description="Sovereign Policy Assistant")
    parser.add_argument("question", help="The question to ask")
    parser.add_argument("--department", default=None, help="Asking staff member's department")
    parser.add_argument("--json", action="store_true", help="Print raw JSON result")
    args = parser.parse_args()

    result = ask(args.question, department=args.department)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(f"\n[{result['case']}] ({result['language']})")
    print(result["answer"])
    if result["citations"]:
        print("\nSource:")
        for c in result["citations"]:
            note = f" — {c['governs_note']}" if c["governs_note"] else ""
            print(
                f"  {c['title']} ({c['doc_id']}, v{c['version']}, "
                f"effective {c['effective_date']}, status: {c['status']})\n"
                f"  Approved by: {c['approver_name']}, {c['approver_role']}{note}"
            )


if __name__ == "__main__":
    main()
