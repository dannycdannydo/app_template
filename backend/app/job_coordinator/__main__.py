"""``python -m app.job_coordinator`` entry point (durable delivery plan P3)."""

from app.job_coordinator.loop import main

if __name__ == "__main__":
    main()
