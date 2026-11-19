"""Randomized, polynomial-time matching for Secret Santa assignments."""
import secrets


class ImpossibleDraw(ValueError):
    pass


def draw(ids: list[int], forbidden: set[tuple[int, int]] | None = None) -> dict[int, int]:
    ids = list(dict.fromkeys(ids))
    if len(ids) < 2:
        raise ImpossibleDraw("Для жеребьёвки нужны минимум два участника")
    forbidden = forbidden or set()
    rng = secrets.SystemRandom()
    # A single cycle avoids mutual pairs. Keep this search bounded: some valid
    # matchings cannot form one cycle, and unbounded backtracking can stall.
    for _ in range(32):
        order = ids[:]
        rng.shuffle(order)
        cycle = {order[i]: order[(i + 1) % len(order)] for i in range(len(order))}
        if all((giver, receiver) not in forbidden for giver, receiver in cycle.items()):
            return cycle

    options = {giver: [receiver for receiver in ids
                       if receiver != giver and (giver, receiver) not in forbidden]
               for giver in ids}
    if any(not choices for choices in options.values()):
        raise ImpossibleDraw("Для одного из участников нет допустимого получателя")
    for choices in options.values():
        rng.shuffle(choices)
    givers = ids[:]
    rng.shuffle(givers)
    receiver_to_giver: dict[int, int] = {}

    def augment(giver: int, seen: set[int]) -> bool:
        for receiver in options[giver]:
            if receiver in seen:
                continue
            seen.add(receiver)
            previous = receiver_to_giver.get(receiver)
            if previous is None or augment(previous, seen):
                receiver_to_giver[receiver] = giver
                return True
        return False

    for giver in givers:
        if not augment(giver, set()):
            raise ImpossibleDraw("Ограничения несовместимы: попробуйте убрать запреты пар")
    return {giver: receiver for receiver, giver in receiver_to_giver.items()}
