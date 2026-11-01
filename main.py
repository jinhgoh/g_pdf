"""Windows / PyInstaller entry point."""

import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from gpdf.app import main

    raise SystemExit(main())
