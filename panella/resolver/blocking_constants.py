"""Frozen blocking vocabulary shared by registry validation and later blocking work."""

from __future__ import annotations

BLOCKING_STOPWORDS = frozenset({
    "i", "me", "we", "us", "you", "they", "he", "she", "it", "is", "am", "are", "was", "were",
    "be", "been", "being", "have", "has", "had", "do", "does", "did", "will", "would", "shall",
    "should", "can", "could", "may", "might", "must", "to", "from", "with", "without", "at", "on",
    "off", "by", "as", "and", "or", "but", "nor", "not", "no", "yes", "this", "that", "these",
    "those", "there", "here", "so", "such", "very", "really", "just", "also", "about", "into", "onto",
    "over", "under", "after", "before", "when", "while", "since", "because", "though", "although", "if",
    "then", "than", "too", "again", "still", "only", "even", "much", "many", "more", "most", "some",
    "any", "all", "both", "each", "every", "other", "another", "same", "via", "per", "use", "uses",
    "used", "using", "like", "likes", "liked", "want", "wants", "wanted", "get", "gets", "got", "gotten",
    "make", "makes", "made", "take", "takes", "took", "go", "goes", "went", "going", "say", "says",
    "said", "tell", "tells", "told", "know", "knows", "knew", "think", "thinks", "thought",
})
