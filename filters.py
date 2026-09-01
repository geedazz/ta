#!/usr/bin/env python3
"""Bendri filtrai, naudojami ir projektu (fetch.py), ir teises aktu (tar.py) srautuose."""

import os
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fold(s: str) -> str:
    """Nuima diakritika ir mazina raides, kad 'silumos' rastu 'silumos'."""
    return "".join(
        c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn"
    ).lower()


def load_list(filename: str):
    path = os.path.join(ROOT, filename)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.startswith("#")]


def in_scope(texts, municipalities) -> bool:
    """Ar irasas patenka i stebimu savivaldybiu rata.

    Savivaldybes teises aktai ir projektai yra aktualus - pvz. techniniu
    prieziuros maksimaliu tarifu sprendimai priimami butent savivaldos lygmeniu.
    Bet aktualios tik konkrecios savivaldybes, isvardytos savivaldybes.txt.

    Nesavivaldybiniai irasai (ministerijos, Vyriausybe, Seimas) visada patenka.
    Tuscias savivaldybiu sarasas reiskia, kad ribojimo nera.
    """
    blob = fold(" ".join(t for t in texts if t))
    if "savivaldyb" not in blob:
        return True
    if not municipalities:
        return True
    return any(fold(m) in blob for m in municipalities)


def excluded(title: str, phrases) -> bool:
    """Ar pavadinimas turi frazes, kuri panaikina atitikma."""
    t = fold(title)
    return any(fold(p) in t for p in phrases)
