import argparse
import asyncio

from .schemas import IgnoredOutput, TriageInput
from .skill import ImplicitHealthTriageSkill


async def run(args):
    async with ImplicitHealthTriageSkill() as skill:
        result = await skill.handle(
            TriageInput(
                session_id=args.session_id,
                turn_id=args.turn_id,
                text=args.text,
                is_final=not args.partial,
            )
        )
        print((result if result is not None else IgnoredOutput()).model_dump_json(indent=2))


def main():
    parser = argparse.ArgumentParser(description="Task 2 final-text triage")
    parser.add_argument("text")
    parser.add_argument("--session-id", default="demo")
    parser.add_argument("--turn-id", default="t1")
    parser.add_argument("--partial", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
