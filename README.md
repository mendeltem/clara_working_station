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
| **Clé** | model on the Charité model hub, called from the workstation | no | yes | interpreting the request, splitting it into questions and tasks |
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
