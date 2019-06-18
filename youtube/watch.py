from youtube import yt_app
from youtube import util, html_common, comments, local_playlist
import settings

from flask import request
import flask

from youtube_dl.YoutubeDL import YoutubeDL
from youtube_dl.extractor.youtube import YoutubeError
import json
import html
import gevent
import os


def get_related_items_html(info):
    result = ""
    for item in info['related_vids']:
        if 'list' in item:  # playlist:
            item = watch_page_related_playlist_info(item)
            result += html_common.playlist_item_html(item, html_common.small_playlist_item_template)
        else:
            item = watch_page_related_video_info(item)
            result += html_common.video_item_html(item, html_common.small_video_item_template)
    return result

    
# json of related items retrieved directly from the watch page has different names for everything
# converts these to standard names
def watch_page_related_video_info(item):
    result = {key: item[key] for key in ('id', 'title', 'author')}
    result['duration'] = util.seconds_to_timestamp(item['length_seconds'])
    try:
        result['views'] = item['short_view_count_text']
    except KeyError:
        result['views'] = ''
    result['thumbnail'] = util.get_thumbnail_url(item['id'])
    return result
    
def watch_page_related_playlist_info(item):
    return {
        'size': item['playlist_length'] if item['playlist_length'] != "0" else "50+",
        'title': item['playlist_title'],
        'id': item['list'],
        'first_video_id': item['video_id'],
        'thumbnail': util.get_thumbnail_url(item['video_id']),
    }

def get_video_sources(info):
    video_sources = []
    for format in info['formats']:
        if format['acodec'] != 'none' and format['vcodec'] != 'none':
            video_sources.append({
                'src': format['url'],
                'type': 'video/' + format['ext'],
            })

    return video_sources

def get_subtitle_sources(info):
    sources = []
    default_found = False
    default = None
    for language, formats in info['subtitles'].items():
        for format in formats:
            if format['ext'] == 'vtt':
                source = {
                    'url': '/' + format['url'],
                    'label': language,
                    'srclang': language,

                    # set as on by default if this is the preferred language and a default-on subtitles mode is in settings
                    'on': language == settings.subtitles_language and settings.subtitles_mode > 0,
                }

                if language == settings.subtitles_language:
                    default_found = True
                    default = source
                else:
                    result.append(source)
                break

    # Put it at the end to avoid browser bug when there are too many languages
    # (in firefox, it is impossible to select a language near the top of the list because it is cut off)
    if default_found:
        sources.append(default)

    try:
        formats = info['automatic_captions'][settings.subtitles_language]
    except KeyError:
        pass
    else:
        for format in formats:
            if format['ext'] == 'vtt':
                sources.append({
                    'url': '/' + format['url'],
                    'label': settings.subtitles_language + ' - Automatic',
                    'srclang': settings.subtitles_language,

                    # set as on by default if this is the preferred language and a default-on subtitles mode is in settings
                    'on': settings.subtitles_mode == 2 and not default_found,

                })

    return sources


def get_music_list_html(music_list):
    if len(music_list) == 0:
        music_list_html = ''
    else:
        # get the set of attributes which are used by atleast 1 track
        # so there isn't an empty, extraneous album column which no tracks use, for example
        used_attributes = set()
        for track in music_list:
            used_attributes = used_attributes | track.keys()

        # now put them in the right order
        ordered_attributes = []
        for attribute in ('Artist', 'Title', 'Album'):
            if attribute.lower() in used_attributes:
                ordered_attributes.append(attribute)

        music_list_html = '''<hr>
<table>
<caption>Music</caption>
<tr>
'''
        # table headings
        for attribute in ordered_attributes:
            music_list_html += "<th>" + attribute + "</th>\n"
        music_list_html += '''</tr>\n'''

        for track in music_list:
            music_list_html += '''<tr>\n'''
            for attribute in ordered_attributes:
                try:
                    value = track[attribute.lower()]
                except KeyError:
                    music_list_html += '''<td></td>'''
                else:
                    music_list_html += '''<td>''' + html.escape(value) + '''</td>'''
            music_list_html += '''</tr>\n'''
        music_list_html += '''</table>\n'''
    return music_list_html




def extract_info(downloader, *args, **kwargs):
    try:
        return downloader.extract_info(*args, **kwargs)
    except YoutubeError as e:
        return str(e)




@yt_app.route('/watch')
def get_watch_page():
    video_id = request.args['v']
    if len(video_id) < 11:
        abort(404)
        abort(Response('Incomplete video id (too short): ' + video_id))

    lc = request.args.get('lc', '')
    if settings.route_tor:
        proxy = 'socks5://127.0.0.1:9150/'
    else:
        proxy = ''
    yt_dl_downloader = YoutubeDL(params={'youtube_include_dash_manifest':False, 'proxy':proxy})
    tasks = (
        gevent.spawn(comments.video_comments, video_id, int(settings.default_comment_sorting), lc=lc ),
        gevent.spawn(extract_info, yt_dl_downloader, "https://www.youtube.com/watch?v=" + video_id, download=False)
    )
    gevent.joinall(tasks)
    comments_html, info = tasks[0].value, tasks[1].value

    if isinstance(info, str): # youtube error
        return flask.render_template('error.html', header = html_common.get_header, error_mesage = info)

    video_info = {
        "duration": util.seconds_to_timestamp(info["duration"]),
        "id":       info['id'],
        "title":    info['title'],
        "author":   info['uploader'],
    }

    upload_year = info["upload_date"][0:4]
    upload_month = info["upload_date"][4:6]
    upload_day = info["upload_date"][6:8]
    upload_date = upload_month + "/" + upload_day + "/" + upload_year
    
    if settings.enable_related_videos:
        related_videos_html = get_related_items_html(info)
    else:
        related_videos_html = ''


    if settings.gather_googlevideo_domains:
        with open(os.path.join(settings.data_dir, 'googlevideo-domains.txt'), 'a+', encoding='utf-8') as f:
            url = info['formats'][0]['url']
            subdomain = url[0:url.find(".googlevideo.com")]
            f.write(subdomain + "\n")


    download_formats = []

    for format in info['formats']:
        download_formats.append({
            'url': format['url'],
            'ext': format['ext'],
            'resolution': yt_dl_downloader.format_resolution(format),
            'note': yt_dl_downloader._format_note(format),
        })

    return flask.render_template('watch.html',
        header_playlist_names   = local_playlist.get_playlist_names(),
        uploader_channel_url    = '/' + info['uploader_url'],
        upload_date             = upload_date,
        views           = (lambda x: '{:,}'.format(x) if x is not None else "")(info.get("view_count", None)),
        likes           = (lambda x: '{:,}'.format(x) if x is not None else "")(info.get("like_count", None)),
        dislikes        = (lambda x: '{:,}'.format(x) if x is not None else "")(info.get("dislike_count", None)),
        download_formats        = download_formats,
        video_info              = json.dumps(video_info),
        video_sources           = get_video_sources(info),
        subtitle_sources        = get_subtitle_sources(info),

        # TODO: refactor these
        related                 = related_videos_html,
        comments                = comments_html,
        music_list              = get_music_list_html(info['music_list']),

        title       = info['title'],
        uploader    = info['uploader'],
        description = info['description'],
        unlisted    = info['unlisted'],
    )
