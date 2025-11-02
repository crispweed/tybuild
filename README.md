# Setup

CD into this repo in Windows powershell.

Install with pip into a virtual environment, in editable mode, as follows:

```
 python -m venv venv
  .\venv\Scripts\Activate.ps1
  pip install -e .
```

You should then be able to run 'tybuild' from the command line.
(Use 'tybuild -h' to get command line help.)

With the virtual environment active, you should also be able to import stuff from 'tybuild' inside other python code.

When finished, the virtual environment can be deactivated with 'deactivate'.
