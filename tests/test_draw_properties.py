"""Independent small-case oracle for constrained matching."""

from itertools import permutations
from random import Random

import pytest

from santa.draw import ImpossibleDraw, draw


def test_random_constraints_agree_with_brute_force():
    ids = list(range(6))
    rng = Random(20261119)
    for _ in range(100):
        forbidden = {(giver, receiver) for giver in ids for receiver in ids
                     if rng.random() < 0.55}
        possible = any(all(giver != receiver and (giver, receiver) not in forbidden
                           for giver, receiver in zip(ids, receivers))
                       for receivers in permutations(ids))
        if not possible:
            with pytest.raises(ImpossibleDraw):
                draw(ids, forbidden)
        else:
            result = draw(ids, forbidden)
            assert set(result) == set(result.values()) == set(ids)
            assert all(giver != receiver and (giver, receiver) not in forbidden
                       for giver, receiver in result.items())
