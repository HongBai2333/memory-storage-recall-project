import re
import unicodedata

TOKENIZER_VERSION = "nfkc-cjk-unigram-bigram-ascii-v1"


def terms(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).lower()
    result = []
    for match in re.finditer(r"[\u3400-\u9fff\u3040-\u30ff]+|[a-z0-9_]+(?:[-.][a-z0-9_]+)*", text):
        word = match.group()
        if re.match(r"[\u3400-\u9fff\u3040-\u30ff]", word):
            result.extend(word)
            result.extend(word[i : i + 2] for i in range(len(word) - 1))
        else:
            result.append(word)
    return result


def matches(query: str, document: str) -> bool:
    return bool(set(terms(query)) & set(terms(document)))
