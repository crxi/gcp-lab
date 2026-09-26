# Google Cloud network and model tests

This repository runs two tests on Google Cloud virtual machines (VMs):

| Test | What you learn | Cloud hardware |
|---|---|---|
| [ping-latency](experiments/ping-latency/README.md) | How long a small packet takes to travel to another VM and back | Two small CPU VMs |
| [llm-chat](experiments/llm-chat/README.md) | How quickly a language model starts answering and generates text as a conversation grows | One NVIDIA L4 GPU VM and one small CPU VM |

Start with ping-latency. It checks that your account, VM creation, and remote
connections work before you add the model and GPU requirements. Your own
computer does not need a GPU.

The scripts create billable Google Cloud resources. They normally delete the
VMs and their boot disks when finished. The model test keeps a reusable VM
image, which you can delete separately. [COSTS.md](COSTS.md) explains the
resource charges.

## Prepare your computer and Google Cloud project

Use a macOS or Linux terminal, or a Linux environment such as WSL on Windows.
You need Git, Python 3.11 or later, an SSH client, and the
[Google Cloud CLI (`gcloud`)](https://docs.cloud.google.com/sdk/docs/install-sdk).
The commands below run on your own computer.

Choose a Google Cloud project with billing enabled. A project groups your
VMs, permissions, and quotas. These scripts use its network named `default`.
If that network has been removed, ask the project administrator to provide a
suitable default network before continuing.

Your account needs permission to create and delete VMs, disks, images, and
firewall rules. Remote commands use
[Identity-Aware Proxy (IAP)](https://docs.cloud.google.com/compute/docs/connect/ssh-using-iap),
which connects to VMs without giving them public IP addresses. You also need
[OS Login with administrator access](https://docs.cloud.google.com/compute/docs/oslogin/set-up-oslogin)
for commands that use `sudo`. In a managed project, ask your administrator to
check these permissions before starting.

Download this repository and install its Python dependencies:

```bash
git clone https://github.com/crxi/gcp-lab.git
cd gcp-lab
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install 'google-cloud-compute>=1.19' 'google-auth>=2.30' 'requests>=2.31'
```

Sign in to the same account for both commands. The first signs in the
`gcloud` tool; the second creates the
[application credentials](https://docs.cloud.google.com/docs/authentication/set-up-adc-local-dev-environment)
used by the Python scripts. Follow the browser prompts.

```bash
gcloud auth login
gcloud auth application-default login
```

Replace `YOUR_PROJECT_ID` below with the project's ID, not its display name
or numeric project number. If you cannot enable APIs, ask your administrator
to run the `services enable` command.

```bash
gcloud config set project YOUR_PROJECT_ID
gcloud auth application-default set-quota-project YOUR_PROJECT_ID
gcloud services enable compute.googleapis.com iap.googleapis.com cloudbilling.googleapis.com --project YOUR_PROJECT_ID
export PATH="$HOME/.local/bin:$PATH"
./setup.sh
glab project YOUR_PROJECT_ID
glab zone asia-southeast1-a
```

`glab` is this repository's command-line tool for managing test VMs.
`setup.sh` installs its launcher in `~/.local/bin` and checks dependencies
and credentials. `asia-southeast1` is the Singapore region;
`asia-southeast1-a` is one zone within it. Use that zone for the first run.

Check the configuration:

```bash
./setup.sh --check
glab whoami
gcloud compute networks describe default --project YOUR_PROJECT_ID --format='value(name)'
```

Continue when setup reports `ready`, `whoami` shows your intended project
and zone, and the network command prints `default`.

## Check your first remote connection

Run this once from an interactive terminal. It lets `gcloud` create an SSH
key and ask any first-connection questions before a test runs unattended.

```bash
glab init connection-check --max-run 10
```

Wait about 30 seconds after creation, then run:

```bash
gcloud compute ssh connection-check --zone asia-southeast1-a --project YOUR_PROJECT_ID --tunnel-through-iap --command 'echo connected'
```

Follow any key-creation prompts. If you protect the key with a passphrase,
make sure your SSH agent has unlocked it before running the experiments.
An IAP `4003` error immediately after creation can mean SSH is still starting;
wait another 30 seconds and retry. For persistent access errors, check IAP
and OS Login permissions using the guides above.

When the command prints `connected`, remove the check VM. Type `y` at the
confirmation prompt:

```bash
glab destroy connection-check
```

## Run the tests

From the repository directory, run the network test first:

```bash
python3 experiments/ping-latency/run.py
```

Then follow the [llm-chat guide](experiments/llm-chat/README.md) to check GPU
quota and run the model test. The [experiment overview](experiments/README.md)
explains what each test measures and where results are saved.

In a new terminal, return to this repository and activate the environment
before using these commands:

```bash
source .venv/bin/activate
export PATH="$HOME/.local/bin:$PATH"
```

## Get help from an LLM

An LLM that can read this repository can guide you through setup and explain
errors. Give it this README and the guide for the test you want to run. For
example:

> Help me run ping-latency for the first time. Read the setup and experiment
> guides, check what is installed, explain each command, and help me verify
> the results and cleanup. Start with the default CPU configuration.

For llm-chat, ask it to check L4 quota, help you watch progress, and explain
the timing fields. Share the command and relevant error text when something
fails; keep credentials, private keys, and login codes private.

## Inspect and remove resources

```bash
glab list --all-zones
glab images
```

To remove a remaining VM, set its zone from the listing, then destroy it:

```bash
glab zone ZONE_FROM_LIST
glab destroy INSTANCE_NAME
```

`glab destroy --all` affects lab VMs in the working zone only. Check the list
before using it. `glab image-delete IMAGE_NAME` removes a saved model image;
the next model test will rebuild it.

## Repository files

| Path | Purpose |
|---|---|
| `glab.py` | VM, image, quota, and price commands; run `glab --help` for the list |
| `setup.sh` | Install the launcher and check local dependencies |
| `experiments/` | Test scripts, guides, and saved results |
| `tests/` | Local tests; run `python3 -m unittest discover -s tests` |
| `COSTS.md` | Resource pricing and worked estimates |

The local tests mock cloud API calls. They do not launch VMs.
