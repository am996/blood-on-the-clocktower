"""Built-in role catalogue for the prototype's server-authoritative rules engine."""

def role(role_id, name, team, sequence, ability, **extra):
    return {"id": role_id, "name": name, "team": team, "night_order": sequence,
            "ability": ability, **extra}


BUILTIN_ROLES = {
    "painter": role("painter", "Painter", "Townsfolk", 0, "Once per game, ask the Storyteller one yes-or-no question."),
    "investigator": role("investigator", "Investigator", "Townsfolk", 1, "On your first night, learn that one of two players is a named Minion.", first_night=True, info=True),
    "watchman": role("watchman", "Watchman", "Townsfolk", 2, "On your first night, learn one player who is a Minion.", first_night=True, info=True),
    "exorcist": role("exorcist", "Exorcist", "Townsfolk", 3, "Choose a player. If they are a Demon, they cannot kill tonight.", targets=1),
    "innkeeper": role("innkeeper", "Innkeeper", "Townsfolk", 4, "Choose two players. They are safe tonight; one becomes drunk until dusk.", targets=2),
    "bodyguard": role("bodyguard", "Bodyguard", "Townsfolk", 5, "Choose a player. If they would die tonight, you die instead.", targets=1),
    "monk": role("monk", "Monk", "Townsfolk", 6, "Choose another player. They are safe from Demon attacks tonight.", targets=1),
    "alchemist": role("alchemist", "Alchemist", "Townsfolk", 10, "Choose a player. Learn whether they are Good or Evil.", targets=1, info=True),
    "bounty_hunter": role("bounty_hunter", "Bounty Hunter", "Townsfolk", 11, "Learn one living player who is not the Demon.", info=True),
    "gossip": role("gossip", "Gossip", "Townsfolk", 19, "Make one public statement during the day. If true, someone may die tonight."),
    "mayor": role("mayor", "Mayor", "Townsfolk", 20, "If three players live and nobody is executed, Good wins. Your night death may be redirected."),
    "oracle": role("oracle", "Oracle", "Townsfolk", 22, "Learn how many dead players are evil.", info=True),
    "scarlet_woman": role("scarlet_woman", "Scarlet Woman", "Minion", 23, "If the Demon dies with 5+ living players, you become the Demon."),
    "gravedigger": role("gravedigger", "Gravedigger", "Townsfolk", 24, "If someone was executed today, learn their role.", info=True),
    "sweetheart": role("sweetheart", "Sweetheart", "Outsider", 25, "When you die, one player becomes poisoned."),
    "parson": role("parson", "Parson", "Townsfolk", 0, "You cannot become drunk or poisoned. If executed, night begins immediately."),
    "drunk": role("drunk", "The Drunk", "Outsider", 0, "You think you are a Townsfolk, but your information is false."),
    "mutant": role("mutant", "Mutant", "Outsider", 0, "If you publicly claim Outsider, the Storyteller may execute you."),
    "baron": role("baron", "Baron", "Minion", 0, "Setup: add two Outsiders."),
    "lunatic": role("lunatic", "Lunatic", "Outsider", 8, "You think you are the Demon. Your chosen victims do not die.", targets=1),
    "poisoner": role("poisoner", "Poisoner", "Minion", 12, "Choose a player. They are poisoned through tomorrow.", targets=1),
    "spy": role("spy", "Spy", "Minion", 13, "See the entire Grimoire.", info=True),
    "glitch": role("glitch", "The Glitch", "Minion", 14, "Choose a player. Their ability fails tomorrow.", targets=1),
    "imp": role("imp", "Imp", "Demon", 17, "Choose a player to kill. You may choose yourself to pass on the Imp.", targets=1),
    "zombuul": role("zombuul", "Zombuul", "Demon", 18, "The first time you die, you appear dead but remain active.", targets=1),
}

DEFAULT_ROLE = role("villager", "Villager", "Townsfolk", 999, "You have no special ability.")
