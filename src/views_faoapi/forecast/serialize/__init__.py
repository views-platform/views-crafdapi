"""Serialize: format estimator output (numpy) into the served column contract.

One reason to change — the published output column shape/names. Carries no statistics and no
geography; it only lays numpy results onto a `(time, entity)`-indexed DataFrame.
"""
