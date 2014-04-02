# Copyright © 2013, 2014 Kyle Mahan
# This file is part of Red Wind.
#
# Red Wind is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Red Wind is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Red Wind.  If not, see <http://www.gnu.org/licenses/>.


from app import app
from ..models import Post, Context
from .. import views
from ..util import autolinker, download_resource

from flask.ext.login import login_required, current_user
from flask import request, redirect, url_for, make_response, jsonify
from rauth import OAuth1Service

import requests
import re
import json
import pytz

from tempfile import mkstemp
from datetime import datetime
from urllib.parse import urljoin


@app.route('/admin/authorize_twitter')
@login_required
def authorize_twitter():
    """Get an access token from Twitter and redirect to the
       authentication page"""
    callback_url = url_for('authorize_twitter2', _external=True)
    try:
        twitter = twitter_client.get_auth_service()
        request_token, request_token_secret = twitter.get_request_token(
            params={'oauth_callback': callback_url})

        return redirect(twitter.get_authorize_url(request_token))
    except requests.RequestException as e:
        return make_response(str(e))


@app.route('/admin/authorize_twitter2')
def authorize_twitter2():
    """Receive the request token from Twitter and convert it to an
       access token"""
    oauth_token = request.args.get('oauth_token')
    oauth_verifier = request.args.get('oauth_verifier')

    try:
        twitter = twitter_client.get_auth_service()
        access_token, access_token_secret = twitter.get_access_token(
            oauth_token, '', method='POST',
            params={'oauth_verifier': oauth_verifier})

        current_user.twitter_oauth_token = access_token
        current_user.twitter_oauth_token_secret = access_token_secret

        current_user.save()
        return redirect(url_for('settings'))
    except requests.RequestException as e:
        return make_response(str(e))


@app.route('/api/syndicate_to_twitter', methods=['POST'])
@login_required
def syndicate_to_twitter():
    try:
        post_id = request.form.get('post_id')
        preview = request.form.get('tweet_preview')
        with Post.writeable(Post.shortid_to_path(post_id)) as post:
            twitter_client.handle_new_or_edit(post, preview)
            post.save()
        return jsonify(success=True, twitter_status_id=post.twitter_status_id,
                       twitter_permalink=post.twitter_url)
    except Exception as e:
        app.logger.exception('posting to twitter')
        response = jsonify(success=False,
                           error="exception while syndicating to Twitter: {}"
                           .format(e))
        return response


@views.fetch_external_post_function
def fetch_external_post(source):
    return twitter_client.fetch_external_post(source)


class TwitterClient:
    def __init__(self):
        self.cached_api = None
        self.cached_config = None
        self.config_fetch_date = None
        self.cached_auth_service = None

    def get_auth_service(self):
        if not self.cached_auth_service:
            key = app.config['TWITTER_CONSUMER_KEY']
            secret = app.config['TWITTER_CONSUMER_SECRET']
            self.cached_auth_service = OAuth1Service(
                name='twitter',
                consumer_key=key,
                consumer_secret=secret,
                request_token_url=
                'https://api.twitter.com/oauth/request_token',
                access_token_url='https://api.twitter.com/oauth/access_token',
                authorize_url='https://api.twitter.com/oauth/authorize',
                base_url='https://api.twitter.com/1.1/')
        return self.cached_auth_service

    def get_auth_session(self):
        service = self.get_auth_service()
        session = service.get_session((current_user.twitter_oauth_token,
                                       current_user.twitter_oauth_token_secret))
        return session

    def repost_preview(self, url):
        if not self.is_twitter_authorized(current_user):
            return

        permalink_re = re.compile(
            "https?://(?:www.)?twitter.com/(\w+)/status(?:es)?/(\w+)")
        match = permalink_re.match(url)
        if match:
            api = self.get_auth_session()
            tweet_id = match.group(2)
            embed_response = api.get('statuses/oembed.json',
                                     params={'id': tweet_id})

            if embed_response.status_code // 2 == 100:
                return embed_response.json().get('html')

    def fetch_external_post(self, source):
        permalink_re = re.compile(
            "https?://(?:www.)?twitter.com/(\w+)/status(?:es)?/(\w+)")
        match = permalink_re.match(source)
        if match:
            api = self.get_auth_session()
            tweet_id = match.group(2)
            status_response = api.get('statuses/show/{}.json'.format(tweet_id))

            if status_response.status_code // 2 != 100:
                app.logger.warn("failed to fetch tweet %s %s", status_response,
                                status_response.content)
                return None

            status_data = status_response.json()

            pub_date = datetime.strptime(status_data['created_at'],
                                         '%a %b %d %H:%M:%S %z %Y')
            if pub_date and pub_date.tzinfo:
                pub_date = pub_date.astimezone(pytz.utc)
            real_name = status_data['user']['name']
            screen_name = status_data['user']['screen_name']
            author_name = real_name
            author_url = status_data['user']['url']
            if author_url:
                author_url = self.expand_link(author_url)
            else:
                author_url = 'http://twitter.com/{}'.format(screen_name)
            author_image = status_data['user']['profile_image_url']
            tweet_text = self.expand_links(status_data['text'])

            return Context(source, source, None, tweet_text,
                           'plain', author_name, author_url,
                           author_image, pub_date)

    def expand_links(self, text):
        return re.sub(autolinker.LINK_REGEX,
                      lambda match: self.expand_link(match.group(0)),
                      text)

    def expand_link(self, url, depth_limit=5):
        if depth_limit > 0:
            app.logger.debug("expanding %s", url)
            r = requests.head(url)
            if r and r.status_code == 301 and 'location' in r.headers:
                url = r.headers['location']
                app.logger.debug("redirected to %s", url)
                url = self.expand_link(url, depth_limit-1)
        return url

    def handle_new_or_edit(self, post, preview):
        if not self.is_twitter_authorized():
            return

        permalink_re = re.compile(
            "https?://(?:www.)?twitter.com/(\w+)/status/(\w+)")
        api = self.get_auth_session()

        # check for RT's
        is_retweet = False
        for share_context in post.share_contexts:
            repost_match = permalink_re.match(share_context.source)
            if repost_match:
                is_retweet = True
                tweet_id = repost_match.group(2)
                result = api.post('statuses/retweet/{}.json'.format(tweet_id),
                                  data={'trim_user': True})
                if result.status_code // 2 != 100:
                    raise RuntimeError("{}: {}".format(result,
                                                       result.content))

        is_favorite = False
        for like_context in post.like_contexts:
            like_match = permalink_re.match(post.like_of)
            if like_match:
                is_favorite = True
                tweet_id = like_match.group(2)
                result = api.post('favorites/create.json',
                                  data={'id': tweet_id, 'trim_user': True})
                if result.status_code // 2 != 100:
                    raise RuntimeError("{}: {}".format(result,
                                                       result.content))

        if not is_retweet and not is_favorite:
            img = views.get_first_image(post.content, post.content_format)

            data = {}
            data['status'] = self.create_status(post, preview, has_media=img)
            data['trim_user'] = True

            if post.location:
                data['lat'] = str(post.location.latitude)
                data['long'] = str(post.location.longitude)

            for reply_context in post.reply_contexts:
                reply_match = permalink_re.match(reply_context.source)
                if reply_match:
                    data['in_reply_to_status_id'] = reply_match.group(2)
                    break

            if img:
                tempfile = self.download_image_to_temp(img)
                app.logger.debug(json.dumps(data, indent=True))
                result = api.post('statuses/update_with_media.json',
                                  header_auth=True,
                                  use_oauth_params_only=True,
                                  data=data,
                                  files={'media[]': open(tempfile, 'rb')})

            else:
                result = api.post('statuses/update.json', data=data)

            if result.status_code // 2 != 100:
                raise RuntimeError("status code: {}, headers: {}, body: {}"
                                   .format(result.status_code, result.headers,
                                           result.content))

        post.twitter_status_id = result.json().get('id_str')

    def download_image_to_temp(self, url):
        _, tempfile = mkstemp()
        download_resource(
            urljoin(app.config['SITE_URL'], url), tempfile)
        return tempfile


    def is_twitter_authorized(self):
        return current_user and current_user.twitter_oauth_token \
            and current_user.twitter_oauth_token_secret

    def create_status(self, post, preview, has_media):
        """Create a <140 status message suitable for twitter
        """
        if preview:
            # we can skip the shortening algorithm!
            # replace http://kyl.im/XXXXX with short-link
            # replace http://kylewm.com/XXXX/XX/XX/X with regular link
            # TODO don't hardcode this!
            preview = re.sub('http://kyl.im/[X/]+',
                             post.short_permalink, preview)
            preview = re.sub('http://kylewm.com/[X/]+',
                             post.permalink, preview)
            return preview

        # fallback
        return (post.title or post.content)[:116] + " " + post.permalink


twitter_client = TwitterClient()