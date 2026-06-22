"""Enable ``python -m devready`` as an alternative to the ``devready`` script.

Useful during development before the package is installed, or in environments
where the console script isn't on PATH.
"""

from .cli import app

if __name__ == "__main__":
    app()
