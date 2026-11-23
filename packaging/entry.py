"""PyInstaller entry point: tray app with no arguments, the CLI with arguments."""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from jobhunter.desktop.app import main
    sys.exit(main())
