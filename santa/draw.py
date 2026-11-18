"""Uniformly shuffled backtracking for constrained perfect matching."""
import secrets


class ImpossibleDraw(ValueError):
    pass


def draw(ids: list[int], forbidden: set[tuple[int, int]] | None = None) -> dict[int, int]:
    ids = list(dict.fromkeys(ids))
    if len(ids) < 2:
        raise ImpossibleDraw("Для жеребьёвки нужны минимум два участника")
    forbidden = forbidden or set()
    rng = secrets.SystemRandom()
    order = ids[:]
    rng.shuffle(order)
    options = {giver: [r for r in ids if r != giver and (giver, r) not in forbidden] for giver in ids}
    if any(not choices for choices in options.values()):
        raise ImpossibleDraw("Для одного из участников нет допустимого получателя")
    # Prefer one long cycle when possible; it prevents mutual pairs for 3+ players.
    def solve(single_cycle: bool) -> dict[int, int] | None:
        assigned: dict[int, int] = {}
        used: set[int] = set()

        def visit(left: list[int]) -> bool:
            if not left:
                if not single_cycle:
                    return True
                seen = {ids[0]}
                node = assigned[ids[0]]
                while node not in seen:
                    seen.add(node)
                    node = assigned[node]
                return node == ids[0] and len(seen) == len(ids)
            giver = min(left, key=lambda x: sum(r not in used for r in options[x]))
            choices = [r for r in options[giver] if r not in used]
            rng.shuffle(choices)
            for receiver in choices:
                assigned[giver] = receiver
                used.add(receiver)
                if visit([x for x in left if x != giver]):
                    return True
                used.remove(receiver)
                del assigned[giver]
            return False

        return assigned if visit(order) else None

    result = solve(True) if len(ids) > 2 else None
    result = result or solve(False)
    if result is None:
        raise ImpossibleDraw("Ограничения несовместимы: попробуйте убрать запреты пар")
    return result
