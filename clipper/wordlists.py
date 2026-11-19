"""Moderation word lists. Matched case-insensitively on word boundaries; extend as needed."""

# Hate-speech slurs: an instant block on every platform (hate-speech strikes are the fastest way to a ban).
SLURS = [
    r"n[i1]gg(?:a|er|ah|uh)s?", r"f[a@]gg?(?:ot)?s?", r"retard(?:ed|s)?", r"tr[a@]nn(?:y|ies)",
    r"ch[i1]nks?", r"sp[i1]cs?", r"k[i1]kes?", r"wetbacks?", r"g[o0]oks?", r"dykes?",
    r"coons?", r"beaners?", r"towel ?heads?", r"sand ?n[i1]gg(?:er|a)s?",
]

# Strong profanity: fine on Instagram/Twitch; on YouTube, swearing in the first ~7s limits ads.
PROFANITY = [
    r"fuck\w*", r"motherfuck\w*", r"shit\w*", r"bitch\w*", r"cunts?", r"dicks?", r"puss(?:y|ies)",
    r"cocks?", r"assholes?", r"bastards?", r"whores?", r"sluts?",
]

# Sexual-content keywords in title or speech: suggests the clip is about sexual content.
SUGGESTIVE = [
    r"strip ?club", r"strippers?", r"lap ?dances?", r"private dances?", r"pole ?danc\w*", r"nude\w*",
    r"naked", r"topless", r"boobs?", r"tits", r"sex\w*", r"hookers?", r"prostitut\w*", r"onlyfans",
    r"brothel", r"twerk\w*", r"horny", r"thirsty", r"gyatt",
]
