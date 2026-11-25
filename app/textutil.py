"""Small text helpers shared by search, RAG and price reference."""
import re

STOP = set(
    "the a an is are of in on for to and or do does i you me my what how which who where when can it this that "
    "with at from be there any have has your our".split()
)


def stem(w):
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith("oes"):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def tokens(text):
    return [stem(w) for w in re.findall(r"[a-z0-9]+", text.lower())]


def clean_text(s, n=80):
    """One line, no control characters, limited length. Used on anything a user typed before it reaches an AI prompt."""
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", str(s))
    return re.sub(r"\s+", " ", s).strip()[:n]

