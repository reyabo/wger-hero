# wger-hero

A small self-hosted gamification layer for wger.

`wger-hero` reads workout logs from a self-hosted wger instance through the wger API and turns real training into quests, XP, levels, streaks and achievements.

wger remains the source of truth for workouts, exercises, routines and logs.
wger-hero only adds a game layer on top.

## Goal

The project should make consistent training feel more playful without corrupting the actual training data.

Example:

* Complete a workout in wger → gain XP
* Complete all three HOME HERO × SUPERMOVER 3 days in one week → finish a weekly quest
* Complete a conditioning finisher → gain stamina XP
* Log RIR honestly → gain discipline XP
* Train four weeks consistently → unlock an achievement

## Planned Features

### Core

* Connect to a wger instance via API token
* Sync completed workout logs
* Store local gamification state in SQLite
* Avoid duplicate XP through stable sync IDs
* Display current level, XP, streak and active quests
* Show recent completed workouts and awarded XP
* Run as Docker container

### Quest System

Initial quest types:

* Daily quest: complete today’s planned workout
* Weekly quest: complete all 3 HOME HERO × SUPERMOVER 3 sessions
* Conditioning quest: complete finisher work
* Consistency quest: train multiple weeks without missing the minimum target
* Movement quest: complete carries, crawls, jumps or core work

### XP Categories

Suggested attributes:

* Strength
* Push
* Pull
* Legs
* Stamina
* Mobility
* Discipline

### Achievements

Examples:

* First Workout
* Full Week Hero
* Sandbag Squire
* Ring Adept
* Hollow Rocker
* Three Week Streak
* Supermover Initiate

## Non-Goals

This app should not replace wger.

It should not:

* create or edit workouts in wger in the first version
* modify wger exercise data
* require public internet exposure
* store the wger token in Git
* use cloud services
* assume a specific public wger version without checking API behavior

## Suggested Stack

* Python
* FastAPI
* Jinja2 templates
* HTMX or minimal vanilla JavaScript
* SQLite
* httpx
* pydantic
* pytest
* Docker
* docker compose

## First Version Acceptance Criteria

Version 0.1 is considered useful when it can:

1. connect to wger using an API token
2. fetch recent workout logs
3. detect already processed logs
4. award XP once per completed workout
5. store XP and achievements in SQLite
6. show a simple dashboard
7. run in Docker
8. survive container recreation without losing data
9. provide tests for sync, XP calculation and deduplication
10. avoid storing secrets in the repository

## Design Principle

wger-hero should reward consistency, not reckless overtraining.

The app should never encourage training through pain, ignoring form breakdown or chasing volume at all costs. Technical work, recovery and honest logging should be rewarded too.
