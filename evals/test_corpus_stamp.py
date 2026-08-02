# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A reused corpus must prove it was built the way this run wants it.

`--reuse-vaults` used to mean "the directory exists", which adopts a corpus
distilled under a different prompt, a different model, or an ingest that died
halfway, and reports the result as a frozen baseline. The stamp records every
field that decided the corpus and the reuse path demands an exact match on all
of them; anything else re-ingests from zero. There is deliberately no
partial-resume and no migration — a corpus that half matches is not a corpus.
"""
import json

from evals import _shared


FIELDS = {"version": 1, "distill": True, "lens_fp": "aaaaaaaaaaaa",
          "worker_model": "m", "sessions": 3}


class TestExactMatch:
    def test_a_matching_stamp_is_reusable(self, tmp_path):
        _shared.write_corpus_stamp(tmp_path, FIELDS, present=3)
        assert _shared.corpus_reusable(tmp_path, FIELDS, present=3)

    def test_every_field_is_load_bearing(self, tmp_path):
        """One loop instead of one test per field: a new field added to the
        stamp is covered the moment it lands in FIELDS."""
        _shared.write_corpus_stamp(tmp_path, FIELDS, present=3)
        for name, value in FIELDS.items():
            changed = dict(FIELDS)
            changed[name] = "some other value" if isinstance(value, str) else 99
            assert not _shared.corpus_reusable(tmp_path, changed, present=3), name

    def test_an_extra_field_this_run_did_not_stamp_refuses(self, tmp_path):
        """Comparison is on the whole record, not a subset: a run that added a
        lever must not silently match a corpus built before the lever existed."""
        _shared.write_corpus_stamp(tmp_path, FIELDS, present=3)
        assert not _shared.corpus_reusable(tmp_path, {**FIELDS, "rerank": True},
                                           present=3)


class TestInternalConsistency:
    def test_a_short_corpus_refuses_even_when_the_fields_match(self, tmp_path):
        """The invariant the old check had no way to see: a killed ingest
        leaves the fields right and the notes missing."""
        _shared.write_corpus_stamp(tmp_path, FIELDS, present=3)
        assert not _shared.corpus_reusable(tmp_path, FIELDS, present=2)

    def test_a_longer_corpus_refuses_too(self, tmp_path):
        """More notes than stamped means someone wrote into the vault; the run
        no longer knows what it is measuring."""
        _shared.write_corpus_stamp(tmp_path, FIELDS, present=3)
        assert not _shared.corpus_reusable(tmp_path, FIELDS, present=4)


class TestMissingAndBroken:
    def test_no_stamp_refuses(self, tmp_path):
        """Every corpus frozen before the stamp existed re-ingests once. That
        is the intended cost: it is the only way to learn how it was built."""
        assert not _shared.corpus_reusable(tmp_path, FIELDS, present=3)

    def test_a_corrupt_stamp_refuses_instead_of_raising(self, tmp_path):
        (tmp_path / "corpus.json").write_text("{half written", encoding="utf-8")
        assert not _shared.corpus_reusable(tmp_path, FIELDS, present=3)

    def test_the_stamp_is_written_atomically_and_readable(self, tmp_path):
        _shared.write_corpus_stamp(tmp_path, FIELDS, present=3)
        stamp = json.loads((tmp_path / "corpus.json").read_text(encoding="utf-8"))
        assert stamp["fields"] == FIELDS
        assert stamp["notes"] == 3


class TestLensFingerprint:
    def test_the_profile_changes_the_lens(self):
        """The field that ties a frozen corpus to the prompt that made it."""
        assert (_shared.lens_fingerprint(profile="default")
                != _shared.lens_fingerprint(profile="extractive"))

    def test_the_same_profile_is_stable_across_calls(self):
        assert (_shared.lens_fingerprint(profile="default")
                == _shared.lens_fingerprint(profile="default"))

    def test_a_verbatim_arm_has_no_lens(self):
        """No distiller call, so no prompt can invalidate the corpus."""
        assert _shared.lens_fingerprint(profile=None, distill=False) == ""
