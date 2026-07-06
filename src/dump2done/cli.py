from __future__ import annotations

import argparse
from pathlib import Path

from dump2done.config.loader import load_config
from dump2done.pipeline.runner import PipelineRunner


def add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default="configs/default.yaml", help="Path to config YAML.")
    parser.add_argument("--job-id", required=True, help="Job identifier.")
    parser.add_argument("--device", help="Override inference device.")
    parser.add_argument("--compute-type", help="Override ASR compute type.")
    parser.add_argument("--model-size", help="Override ASR model size.")
    parser.add_argument("--llm-backend", help="Override LLM backend.")
    parser.add_argument("--llm-model", help="Override LLM model.")
    parser.add_argument("--target-format", choices=["9:16", "1:1", "16:9"], help="Output format.")
    parser.add_argument("--num-clips", type=int, help="Number of clips to export.")
    parser.add_argument("--min-duration", type=float, help="Minimum clip duration in seconds.")
    parser.add_argument("--max-duration", type=float, help="Maximum clip duration in seconds.")
    parser.add_argument("--resume", action="store_true", help="Reuse completed artifacts where possible.")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode.")


def apply_overrides(config: dict, args: argparse.Namespace) -> dict:
    config = dict(config)
    asr = dict(config.get("asr", {}))
    llm = dict(config.get("llm", {}))
    render = dict(config.get("render", {}))
    crop = dict(config.get("crop", {}))
    project = dict(config.get("project", {}))

    if args.device:
        asr["device"] = args.device
    if args.compute_type:
        asr["compute_type"] = args.compute_type
    if args.model_size:
        asr["model_size"] = args.model_size
    if args.llm_backend:
        llm["backend"] = args.llm_backend
    if args.llm_model:
        llm["model"] = args.llm_model
    if args.target_format:
        render["target_format"] = args.target_format
        crop["target_format"] = args.target_format
    if args.num_clips:
        llm["global_top_n"] = args.num_clips
    if args.min_duration is not None:
        llm["min_duration"] = args.min_duration
    if args.max_duration is not None:
        llm["max_duration"] = args.max_duration
    if args.debug:
        project["debug"] = True

    config["asr"] = asr
    config["llm"] = llm
    config["render"] = render
    config["crop"] = crop
    config["project"] = project
    return config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dump2done", description="Dump2Done local MVP CLI.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze", help="Create job and inspect input media.")
    add_common_options(analyze)
    analyze.add_argument("--input", required=True, help="Input video path.")

    for command, help_text in [
        ("transcribe", "Extract audio and transcribe speech."),
        ("select-clips", "Select highlight clip candidates."),
        ("crop", "Generate smart crop tracks."),
        ("subtitle", "Generate ASS subtitles."),
        ("render", "Render selected clips."),
    ]:
        sub = subparsers.add_parser(command, help=help_text)
        add_common_options(sub)

    run_all = subparsers.add_parser("run-all", help="Run the full local MVP pipeline.")
    add_common_options(run_all)
    run_all.add_argument("--input", required=True, help="Input video path.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = apply_overrides(load_config(Path(args.config)), args)
    runner = PipelineRunner(config=config, job_id=args.job_id, resume=args.resume)

    if args.command == "analyze":
        runner.analyze(Path(args.input))
    elif args.command == "transcribe":
        runner.transcribe()
    elif args.command == "select-clips":
        runner.select_clips()
    elif args.command == "crop":
        runner.crop()
    elif args.command == "subtitle":
        runner.subtitle()
    elif args.command == "render":
        runner.render()
    elif args.command == "run-all":
        runner.run_all(Path(args.input))
    else:
        parser.error(f"Unknown command: {args.command}")

    return 0

