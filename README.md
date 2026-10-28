# Clara Working Station

What is computed on **Alita**, and what of it is published.

**<https://mendeltem.github.io/clara_working_station/>**

*Work computed on Alita by the local agent. This is a workbench, not a
portfolio: only what was produced here is listed.*

---

## Workflow

![Workflow: a prompt from a phone or desktop enters the workstation; inside it the Cloud API (Clé) and research agents find methods, then Clara (local LLM) writes code, trains on the GPU and checks the result in a loop until done; the model is scored on the test set against the leaderboard and an HTML QC page goes back to phone and desktop](workflow.png)

One request, read left to right, using "I want to create a WMH segmentation
model, can you research it?" as the example:

1. **Prompt.** The researcher states the goal in plain language, from any
   device: the phone app, a desktop PC or the browser. This is the only step
   that happens outside the workstation.
2. **Research agents.** On the workstation, an agent sends the question to Clé
   (the model on the Charité model hub API), which splits it, and several
   research agents search papers and code for every method and architecture
   that has been used for the task.
3. **Method shortlist.** The findings are merged into candidate methods, for
   example 2D U-Net, 3D U-Net, nnU-Net or BIANCA, each turned into tasks with
   machine-checkable acceptance criteria.
4. **Harness, outer loop.** Takes one method at a time from the queue and moves
   on to the next once it is done.
5. **Harness, inner loop on the workstation GPU.** Clara, the local model,
   writes the code, trains on open data (the WMH Segmentation Challenge
   training set), and an acceptance check counts the facts. If the check fails,
   the task goes back to Clara. The check asks no model.
6. **Evaluation.** Every model is scored on the challenge test set with the
   official metric (Dice, lesion F1) and placed next to the public leaderboard,
   where the 2017 winner reached Dice 0.80.
7. **Result.** The best model, a comparison table and a QC page.

Everything after the prompt runs on the workstation: the calls to Clé, the
research agents, the harness, training and evaluation. Clara sees the data and
has no network access. The research agents reach the network and Clé, and never
see the data.

---

## Why a local workstation

**Shared GPU time is too slow for this kind of work.** On the institute
cluster, a job asking for a whole GPU was scheduled about 9 hours ahead; only
7 of the cards have 80 GB. A small share of a card starts within seconds, but
it only fits small trainings
([measurement, 16 Oct 2026](https://github.com/mendeltem/blackbeard_wikki/blob/main/knowledge/local_llm_operation/findings/hpc-nodes-answer-only-ssh-and-shards-start-at-once.md)).
An agent that builds a model, trains it, finds an error and tries again needs
dozens of such rounds a day. With a queue in between, each round costs hours;
on the workstation it costs minutes.

**A local model can look at the patient data; a cloud model cannot.** Clara
runs on the workstation, so the images never leave the machine. That lets her
do what a remote model may not: open every scan, read every mask, run the
model on the whole cohort, and look at the cases where it fails. She sees far
more of the data than any summary would carry, and she sees it fast.

Local is a route, not a permission. Whether a dataset may be processed by an
AI system at all is decided by its data agreement, and a clause that forbids
it binds the local model too
([rule](https://github.com/mendeltem/blackbeard_wikki/blob/main/knowledge/agent_coordination/rules/three-data-classes-decide-what-may-leave-the-institution.md)).
Where processing is allowed, running it locally keeps the data inside the
institution.

## How the parts are used

| Part | Runs on | Sees patient data | Network | Used for |
|---|---|---|---|---|
| **Prompt** | any device: phone app, desktop PC, browser | no | yes | stating the goal; nothing else happens there |
| **Clé** | Charité on-premise model hub (intranet), called from the workstation | no | yes | interpreting the request, splitting it into questions and tasks |
| **Research agents** | workstation, using Clé | no | yes | searching papers and code for methods and architectures |
| **Clara** | workstation, Qwen3.8-27B via llama.cpp | yes | no | writing the code, training, running and inspecting models on the data |
| **Harness** | workstation | through Clara | no | the outer loop over methods and the inner loop until a task passes |
| **Check** | workstation | counts only | no | acceptance criteria as countable facts; asks no model |
| **Exit guard** | workstation, port 8899 | blocks it | only for checked questions | Clara's only way out: technical questions without data reference |
| **Blackboard** | workstation and repo | no | via the repo | where Clara and the cloud model report progress to each other |

---

## Results

| | |
|---|---|
| **[WMH: eleven methods compared](https://mendeltem.github.io/clara_working_station/qc-wmh-verfahren/)** | One page per patient with FLAIR, reference mask and up to eleven segmentations overlaid, multiplanar and as a 3D rendering. Plus one page per method and a staged comparison with paired tests. |
| **[WMH: U-Net, first version](https://mendeltem.github.io/clara_working_station/qc-unet-wmh/)** | The first QC: three slices per case, 170 cases in one file. Kept as a point of comparison. |

---

## Lessons from the WMH line

Measured on the 110 public test cases of the WMH Segmentation Challenge 2017
(Kuijf et al. 2019, [doi:10.1109/TMI.2019.2905770](https://doi.org/10.1109/TMI.2019.2905770)),
official evaluation, paired over cases with bootstrap intervals. These are our
own measurements, not leaderboard placements. Each row links to its entry in
the agent wiki [blackbeard_wikki](https://github.com/mendeltem/blackbeard_wikki).

| Finding | Number |
|---|---|
| [The brain mask is part of the model.](https://github.com/mendeltem/blackbeard_wikki/blob/main/knowledge/wmh_segmentation/findings/brain-mask-is-part-of-the-model.md) The same model with masks from two HD-BET runs: z-normalisation inside the mask shifts the whole input image, not just the edge. | Dice 0.688 vs 0.719 |
| [A second bias correction helps](https://github.com/mendeltem/blackbeard_wikki/blob/main/knowledge/wmh_segmentation/findings/second-bias-correction-helps-on-mixed-scanners.md), although the data are already corrected. Here `fast -B` acts as intensity harmonisation across scanners and cuts the spread across seeds to about a third. | without it Dice −0.011 [−0.016; −0.006], lesion recall −0.037 |
| [The official evaluation deletes predictions on label 2](https://github.com/mendeltem/blackbeard_wikki/blob/main/knowledge/wmh_segmentation/findings/official-label-2-rule-deletes-the-prediction.md) instead of counting them as false positives (`evaluation.py`, lines 47–50). | stricter counting is 0.005–0.007 Dice too low |
| [Unseen scanners are not the weak spot.](https://github.com/mendeltem/blackbeard_wikki/blob/main/knowledge/wmh_segmentation/findings/unseen-scanners-are-not-the-weak-spot.md) The test set has five devices, not three sites; every case below Dice 0.5 is on a known device. | 20 cases on unseen devices, 0 failures |
| [Changing the network as a package brought nothing.](https://github.com/mendeltem/blackbeard_wikki/blob/main/knowledge/wmh_segmentation/findings/architecture-package-brought-no-gain.md) The apparent gain of +0.032 Dice was the mask artefact from the first row. | Dice −0.001, lesion recall −0.038 |

The consequence for every model comparison: record where the mask came from and
which evaluation function was used, and change one component at a time.

---

## The system behind it

Clara is a local model on a single workstation without network access. Tools,
measurements and operating rules are kept separately:

**[local_models](https://github.com/mendeltem/local_models)** · [page](https://mendeltem.github.io/local_models/) · [the model-independent scaffolding](https://github.com/mendeltem/local_models/blob/main/docs/05-scaffolding.md)

What the agents learned along the way is collected in the wiki
**[blackbeard_wikki](https://github.com/mendeltem/blackbeard_wikki)**, section
[local_llm_operation](https://github.com/mendeltem/blackbeard_wikki/blob/main/knowledge/local_llm_operation/SUMMARY.md).

### The machine, October 2026

- RTX A5000 with 24 GB (Ampere, sm_86), i9-12900K, 62 GiB RAM, Ubuntu 24.04,
  NVIDIA driver 550 (CUDA 12.4).
- Clara's model is Qwen3.8-27B in Q4 via llama.cpp. Measured on 23 October on
  the same card against Nemotron 3.5 Bolt 74B-A9B: 100 % vs 88.5 % on a 13-task
  test (97.4 % vs 96.2 % at 4,096 tokens). Bolt generates 66–69 tok/s against
  about 30 but reasons longer, so the wall-time gain shrinks to about 20 %.
  [Entry](https://github.com/mendeltem/blackbeard_wikki/blob/main/knowledge/local_llm_operation/findings/nemotron-bolt-74b-on-ampere-24gb.md)
- The model server unloads when idle: 21,770 MiB VRAM in use, 1,618 MiB after,
  11.4 s to wake. This keeps the card free for training.
- With Secure Boot, a kernel update can disable the NVIDIA driver without any log
  naming it. Before rebooting, check `dkms status` and `mokutil --list-enrolled`.
  [Entry](https://github.com/mendeltem/blackbeard_wikki/blob/main/knowledge/local_llm_operation/risks/secure-boot-kernel-update-unloads-nvidia-dkms.md)

---

## Rebuild it: setup

Everything needed to set up the same system on another machine. Numbers are
measured on Alita unless marked otherwise; versions are those in use in
October 2026. The full scripts are in
[local_models/alita](https://github.com/mendeltem/local_models/tree/main/alita).

### Hardware

| Part | On Alita | What matters |
|---|---|---|
| GPU | NVIDIA RTX A5000, 24 GB (Ampere, compute capability 8.6) | 24 GB holds the 27B model in Q4 at 80k context (~20.8 GiB); bfloat16 yes, FP8 no |
| CPU | Intel i9-12900K, 16 cores / 24 threads | embeddings and the run watcher run on the CPU |
| RAM | 62.6 GiB, 8 GiB swap | `mri_WMHsynthseg` needs ~54 GiB and runs alone |
| Disk | NVMe, 915 GB | models ~85 GB, public datasets, wheels for offline installs |
| Board | MSI MS-7D91 | |

### Operating system and drivers

| | |
|---|---|
| OS | Ubuntu 24.04.3 LTS, kernel 6.14 |
| NVIDIA driver | 550.163.01, i.e. CUDA 12.4 at most; PyTorch wheels up to cu126 run on it |
| Rights | no sudo in daily use: system packages are unpacked with `apt-get download` + `dpkg-deb -x` |
| Services | systemd user units with `loginctl enable-linger`, all listening on loopback only |
| Secure Boot | enrol the MOK key, or a kernel update silently unloads the NVIDIA module |

### Models

| Role | Model | Format | Runs on | Measured |
|---|---|---|---|---|
| Clara, main agent model | Qwen3.8-27B | GGUF `UD-Q4_K_M`, 16 GB, 80k context | GPU, llama.cpp | ~30–33 tok/s, 20.8 GiB VRAM |
| Clara's eyes | vision projector for Qwen3.8-27B | `mmproj-F16.gguf`, 885 MB | loaded with `--mmproj`, projector on the CPU | 16,178 MiB VRAM at 8k context; context length, not the projector, decides out-of-memory |
| Small model beside GPU jobs | Ternary-Bonsai-2-27B | `PQ2_0`, 6.8 GB (PrismML llama.cpp fork) | GPU | 45 tok/s, 92 % on the 13-task test |
| Embeddings | bge-m3 | GGUF `Q8_0` | CPU, port 8001 | 0.23 GiB RAM |
| Run watcher | Qwen2.5-0.5B-Instruct | GGUF `Q8_0` | CPU | |
| Clé, planning and research | Charité on-premise model hub, OpenAI-compatible API | aliases `fast` (Qwen 3.8 Flash Next), `frontier` (DeepSeek V4.1 Flash), `eu` (Mistral Small 4), `light`, `clea` | hospital intranet, called from the workstation | 13-task test: `clea` 100 %, `frontier` 96 %, `fast` 92 % at 180 tok/s |

Reasoning models on the hub need `max_tokens` of about 6,000, otherwise the
hidden reasoning uses up the budget and the answer comes back empty
([entry](https://github.com/mendeltem/blackbeard_wikki/blob/main/knowledge/local_llm_operation/findings/hospital-model-hub-api-aliases-and-small-benchmark.md)).

### Software

| Layer | What | Version / setting |
|---|---|---|
| Inference server | [llama.cpp](https://github.com/ggml-org/llama.cpp), built from source | CUDA build for sm_86 (`cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=86`), commit f64a498 |
| Server environment | conda `qwen-serve` | Python 3.12, gcc 12, CUDA 12.4 |
| Training environment | conda `dl` | Python 3.12, PyTorch 2.6.0+cu124, MONAI 1.5.2, nnU-Net 2.8.1, nibabel, scikit-learn |
| Agent harness | [Hermes Agent](https://github.com/NousResearch/hermes-agent) | v0.21.5; its gateway receives prompts from the phone |
| Coding agent | [OpenCode](https://github.com/sst/opencode) | 1.18.35, pointed at the local server |
| Imaging tools | FSL (`bet`, `flirt`, `fast`, `bianca`), HD-BET, FreeSurfer 8.2.0 (`mri_synthseg`, `mri_WMHsynthseg`) | |
| QC pages | [NiiVue](https://github.com/niivue/niivue) | bundled `niivue.umd.js`, pages work offline |

### Services

| Port | Service | Role |
|---|---|---|
| 8000 | `llama-server` | Clara's model |
| 8001 | `embedding-server` | bge-m3 on the CPU |
| 8800 | `chat-gui` | chat interface |
| 8810 | `qc-viewer` | QC pages |
| 8899 | `guard-daemon` | exit guard, Clara's only way out |
| – | `dauerlauf` | job queue, runs the agent continuously |
| – | `hermes-gateway` | receives prompts from the phone |

### Data

[WMH Segmentation Challenge 2017](https://wmh.isi.uu.nl/): 60 training and 110
test cases (FLAIR + T1, reference masks), [doi:10.34894/AECRSD](https://doi.org/10.34894/AECRSD),
licence CC BY-NC 4.0.

### Steps

1. Install the NVIDIA driver (550 or newer) and check `nvidia-smi`.
2. Install Miniconda and create the two environments `qwen-serve` and `dl`.
3. Build llama.cpp with CUDA in the `qwen-serve` environment (command above).
4. Download the GGUF files from Hugging Face. On Alita downloads stalled at
   about 15.9 MB until `HF_HUB_DISABLE_XET=1` was set.
5. Clone [local_models](https://github.com/mendeltem/local_models) and run
   `alita/einrichten.sh --pruefen`, then `alita/einrichten.sh`. It creates the
   systemd user units and checks paths, environments and weights.
6. Run `system/werkzeuge/werkzeugtest --schnell`. It calls every tool on real
   input, so it says "computes", not just "installed".

---

## What is not here

Patient data and results on in-house cohorts stay on the machine. Only work on
public datasets is published: the QC above runs on the *WMH Segmentation
Challenge* (Utrecht, Singapore, Amsterdam).

This also covers everything derived from such data. A mask, an embedding, a log
line, a QC page or a model trained on it carries the class of its source
([rule](https://github.com/mendeltem/blackbeard_wikki/blob/main/knowledge/agent_coordination/rules/three-data-classes-decide-what-may-leave-the-institution.md)).
That is why the comparisons on the mixed pool of challenge and in-house cohorts
are missing here, even though they are in the wiki.

Checked before every push with `system/werkzeuge/pruefe-veroeffentlichung`:
every dataset needs an explicit release, and a path to unreleased data aborts
the push.
