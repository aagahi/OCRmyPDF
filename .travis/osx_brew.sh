#!/bin/bash
set -euo pipefail
set -x

pip3 install homebrew-pypi-poet
python3 .travis/autobrew.py
cat ocrmypdf.rb
brew audit ocrmypdf.rb

# Extract deploy key for jbarlow83/homebrew-ocrmypdf
openssl aes-256-cbc -K $encrypted_e35043491734_key -iv $encrypted_e35043491734_iv \
    -in .travis/homebrew-ocrmypdf.enc -out homebrew-ocrmypdf_key -d

export GIT_SSH_COMMAND="ssh -i homebrew-ocrmypdf_key"

git clone git@github.com:jbarlow83/homebrew-ocrmypdf.git homebrew

cd homebrew
cp ../ocrmypdf.rb Formula/ocrmypdf.rb
git add Formula/ocrmypdf.rb
git commit -m "homebrew-ocrmypdf: automatic release $TRAVIS_BUILD_NUMBER $TRAVIS_TAG"
git push -v origin master