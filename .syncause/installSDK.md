
Syncause Python SDK Installation Guide

<step_1>
**Install syncause_tracer**

If you are using a .venv environment, replace pip in the following command with .venv/bin/pip:

1. **Using `requirements.txt`**:

   Add the following line to your `requirements.txt`:

   ```
    https://github.com/Syncause/syncause-sdk/releases/download/syncause_tracer_v0.2.7/syncause_tracer-0.2.7-py3-none-any.whl
   ```

   Then, run the following command to install:

   ```
   pip install -r requirements.txt
   ```

2. **Using `pyproject.toml`**:

   If you're using Poetry, add the following to the `[tool.poetry.dependencies]` section of your `pyproject.toml`:

   ```
   [tool.poetry.dependencies]
   python = "^3.7"
   syncause_tracer = { url = "https://github.com/Syncause/syncause-sdk/releases/download/syncause_tracer_v0.2.7/syncause_tracer-0.2.7-py3-none-any.whl" }
   ```

   Then run the following command to install:

   ```
   poetry install
   ```
</step_1>

<step_2>
Locate the main entry file for your project:

Django projects use a settings.py file; if multiple files exist, select the one for your development environment.
Other frameworks will automatically identify the entry file.

Add the following code to the top of your entry file.
```
from syncause_tracer import initialize
initialize(
    api_key="sysk_nIgjXwFbIDmYkLTcUc36nFrZJ7Gpq67",
    proxy="wss://api.syn-cause.com/codeproxy/ws",
    app_name="MTCA",
    project_id="56252d81-82ff-49be-bcc4-2e8e0c1900c4"  # This is your project ID
)
```

</step_2>

<step_3>
**Verify installation**
After completing the installation, start your service.
</step_3>

<important>
- After installation, check whether the application starts and runs normally.
</important>

**Installation Complete**

The installation has been completed.
Please return to the plugin panel on the left to proceed with the next steps.

> **AI Instruction:**
> After installation is complete, output a message instructing the user to return to the plugin panel on the left to proceed with the next steps.


