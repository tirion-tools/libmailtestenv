# Mail test servers (testenv.sh). One image, one container per role:
#   mailtestenv-mail    Dovecot 2.4.5 + Postfix (TLS, SNI, OAuth)
#   mailtestenv-gmail   Dovecot + the Gmail-faithful proxy
#   mailtestenv-enron   Dovecot with the Enron corpus, one user per mailbox owner
#   mailtestenv-apache  Dovecot with the ASF mailing-list dataset (datasets/apache/), user `apache`
# Base pinned by digest; packages from the Arch Linux Archive snapshot of 2026-11-12, every version asserted.
FROM archlinux@sha256:fa2d4ce3310ddd90a4208e5168be8ad367da5129af1245f38fd2d803bcbe3239
RUN echo 'Server = https://archive.archlinux.org/repos/2026/11/12/$repo/os/$arch' > /etc/pacman.d/mirrorlist \
    && pacman -Syu --noconfirm dovecot postfix socat openssl python rsync \
    && pacman -Q dovecot postfix socat openssl python rsync > /opt/testenv-packages.txt \
    && printf '%s\n' 'dovecot 2.4.5-2' 'postfix 3.11.7-2' 'socat 1.8.1.3-2' 'openssl 4.1.0-1' 'python 3.14.10-1' \
         'rsync 3.5.2-3' | cmp - /opt/testenv-packages.txt \
    && pacman -Scc --noconfirm \
    && useradd --system --uid 5000 --user-group --home-dir /srv/mail --shell /usr/bin/nologin vmail \
    && useradd --system --uid 5001 --user-group --home-dir /tmp --shell /usr/bin/nologin testenv
COPY mail/ /opt/testenv/mail/
COPY gmail/ /opt/testenv/gmail/
COPY enron/ /opt/testenv/enron/
COPY apache/ /opt/testenv/apache/
ENTRYPOINT []
