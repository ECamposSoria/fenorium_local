"""Acquisition jobs: scheduled, restartable fetches that publish validated artifacts.

`runner` runs one job; `state` remembers every run and every item; `jobs` names what runs.
Stdlib only, so the same code runs in the platform image and the collector image.
"""
