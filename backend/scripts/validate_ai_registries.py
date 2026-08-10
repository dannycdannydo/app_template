"""Validate checked-in AI registries for the local/CI quality gate."""

from app.ai.registry import load_registry_bundle


def main() -> None:
    bundle = load_registry_bundle()
    print(
        "AI registries valid: "
        f"{len(bundle.tasks.all())} task(s), "
        f"{len(bundle.prompts.all())} prompt version(s), "
        f"{len(bundle.models.all())} model(s)"
    )


if __name__ == "__main__":
    main()
