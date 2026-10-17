#!/bin/sh
# Rock Creek feeds (kept for the deployed Rock Creek config): see twin_feeds.sh
exec sh "$(cd "$(dirname "$0")" && pwd)/twin_feeds.sh" RockCreek
