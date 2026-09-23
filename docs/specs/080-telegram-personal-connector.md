# Telegram Personal Connector

## Purpose

The Telegram bot surface is not enough for personal-account capabilities such as reading the user's recent dialogs or writing as the user.

AISIS therefore defines a separate MTProto connector, preferably running on the Open Remote Commander (ORC) edge transport so the personal Telegram session can stay local.

## Capabilities

V1 supports:
- list/retrieve recent dialogs;
- search recent messages by text/person/chat/time;
- inspect recent context around a message;
- resolve a person/chat from natural language;
- send a message after policy checks;
- learn user-approved aliases and confirmed resolutions.

## Stable identity

All writes resolve to stable Telegram peer identity (numeric peer ID plus required access data).

Usernames and display names are hints and may change.

## Recipient ranking

Candidate ranking combines exact peer/username match, normalized names, aliases, transliteration, fuzzy similarity, dialog recency, message frequency, reply frequency, mutual-chat context, and previously confirmed resolution.

No single fuzzy string score can authorize an ambiguous send.

## Confirmation

A high-confidence known recipient may be auto-selected according to user policy.

Close candidates, changed usernames, conflicting identities, first-time sensitive sends, or suspicious ID/name mismatches require confirmation.

The confirmation UI shows enough context to distinguish candidates without exposing unrelated private history.

## Message search

Search uses the Telegram server/API where possible plus a bounded local index for faster fuzzy/semantic retrieval.

The local index may combine FTS/BM25, normalization/transliteration, metadata scoring, and optional embeddings.

Raw chat history is not sent to an LLM unless the query requires it and the user's connector policy permits it.

## Memory

AISIS may remember “who I usually write to” as derived routing features and explicit aliases.

It must support deleting/resetting learned mappings and must not convert frequency into a claim about personal relationships.
