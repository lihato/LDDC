# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: Copyright (c) 2024 沉默の金
import json
import logging
import os
import re
import time

from PySide6.QtCore import (
    QCoreApplication,
    QEventLoop,
    QObject,
    QRunnable,
    Qt,
    QTimer,
    Signal,
    Slot,
)

from ui.sidebar_window import SidebarWindow
from utils.cache import cache
from utils.data import cfg
from utils.enum import (
    LocalMatchFileNameMode,
    LocalMatchSaveMode,
    LyricsFormat,
    LyricsProcessingError,
    SearchType,
    Source,
)
from utils.threadpool import threadpool
from utils.utils import (
    compare_version_numbers,
    escape_filename,
    escape_path,
    get_lyrics_format_ext,
    get_save_path,
    replace_info_placeholders,
    text_difference,
)

from .api import (
    get_latest_version,
    kg_get_songlist,
    kg_search,
    ne_get_songlist,
    ne_search,
    qm_get_album_song_list,
    qm_get_songlist_song_list,
    qm_search,
)
from .lyrics import Lyrics
from .song_info import file_extensions as audio_formats
from .song_info import get_audio_file_info, parse_cue


class CheckUpdateSignal(QObject):
    show_message = Signal(str, str, str)
    show_new_version_dialog = Signal(str, str)  # 版本号, 版本信息


class CheckUpdate(QRunnable):
    def __init__(self, is_auto: bool, windows: SidebarWindow, version: str) -> None:
        super().__init__()
        self.isAuto = is_auto
        self.windows = windows
        self.version = version
        self.signals = CheckUpdateSignal()

    def run(self) -> None:
        is_success, last_version, body = get_latest_version()
        if is_success:

            if compare_version_numbers(self.version, last_version):
                self.signals.show_new_version_dialog.emit(last_version, body)
            elif not self.isAuto:
                self.signals.show_message.emit("info", QCoreApplication.translate("CheckUpdate", "检查更新"), QCoreApplication.translate("CheckUpdate", "已经是最新版本"))

        elif not self.isAuto:
            self.signals.show_message.emit("error", QCoreApplication.translate("CheckUpdate", "检查更新"), QCoreApplication.translate("CheckUpdate", "检查更新失败，错误:{0}").format(last_version))


class SearchSignal(QObject):
    error = Signal(str)
    result = Signal(int, SearchType, list)


class SearchWorker(QRunnable):

    def __init__(self, taskid: int, keyword: str | dict, search_type: SearchType, source: Source, page: int | str = 1) -> None:
        super().__init__()
        self.taskid = taskid
        self.keyword = keyword  # str: 关键字, dict: 歌曲信息
        self.search_type = search_type
        self.source = source
        self.page = int(page)
        self.signals = SearchSignal()

    def run(self) -> None:
        logging.debug("开始搜索")
        if isinstance(self.keyword, dict):
            keyword = {"keyword": f"{self.keyword['artist']} - {self.keyword['title']}",
                       "duration": self.keyword["duration"], "hash": self.keyword["hash"]}
        else:
            keyword = self.keyword
        if ("serach", str(keyword), self.search_type, self.source, self.page) in cache:
            logging.debug(f"从缓存中获取搜索结果,类型:{self.search_type}, 关键字:{keyword}")
            search_return = cache[("serach", str(keyword), self.search_type, self.source, self.page)]
        else:
            for _i in range(3):
                match self.source:
                    case Source.QM:
                        search_return = qm_search(keyword, self.search_type, self.page)
                    case Source.KG:
                        if isinstance(keyword, dict) and self.search_type == SearchType.LYRICS:
                            search_return = kg_search(keyword, SearchType.LYRICS)
                        else:
                            search_return = kg_search(keyword, self.search_type, self.page)
                    case Source.NE:
                        search_return = ne_search(keyword, self.search_type, self.page)
                if isinstance(search_return, str):
                    continue
                break
            if isinstance(search_return, str):
                self.signals.error.emit(search_return)
                return
            if not search_return:
                self.signals.error.emit("没有任何结果")
                return
            cache[("serach", str(keyword), self.search_type, self.source, self.page)] = search_return

        if self.source == Source.KG and isinstance(self.keyword, dict) and self.search_type == SearchType.LYRICS:
            # 为歌词搜索结果添加歌曲信息(歌词保存需要)
            result = [dict(self.keyword, **item) for item in search_return]
        else:
            result = search_return
        self.signals.result.emit(self.taskid, self.search_type, result)
        logging.debug("发送结果信号")


class LyricProcessingSignal(QObject):
    result = Signal(int, dict)
    error = Signal(str)


class LyricProcessingWorker(QRunnable):

    def __init__(self, task: dict) -> None:
        super().__init__()
        self.task = task
        self.signals = LyricProcessingSignal()
        self.is_running = True

    def run(self) -> None:
        if self.task["type"] == "get_merged_lyric":
            self.taskid = self.task["id"]
            self.get_merged_lyric(self.task["song_info"], self.task["lyric_type"])

        elif self.task["type"] == "get_lyric":
            self.taskid = self.task["id"]
            lyrics, from_cache = self.get_lyrics(self.task["song_info"])
            if isinstance(lyrics, tuple):
                self.signals.result.emit(self.taskid, {"error_str": lyrics[0], "error_type": lyrics[1]})
            else:
                self.signals.result.emit(self.taskid, {"result": lyrics})

        elif self.task["type"] == "get_list_lyrics":
            self.skip_inst_lyrics = cfg["skip_inst_lyrics"]
            for count, song_info in enumerate(self.task["song_info_list"]):
                if not self.is_running:
                    logging.debug("任务被取消")
                    break
                self.count = count + 1
                match song_info['source']:
                    case Source.QM | Source.NE:
                        info = song_info
                    case Source.KG:
                        if self.skip_inst_lyrics and song_info['language'] in ["纯音乐", '伴奏']:
                            self.signals.error.emit(f"{song_info['artist']} - {song_info['title']} 为纯音乐,已跳过")
                            continue
                        search_return = kg_search({"keyword": f"{song_info['artist']} - {song_info['title']}",
                                                   "duration": song_info["duration"],
                                                   "hash": song_info["hash"]},
                                                  SearchType.LYRICS)
                        if isinstance(search_return, str):
                            self.signals.error.emit(QCoreApplication.translate("LyricProcess", "搜索歌词时出现错误{0}").format(search_return))
                            continue
                        if not search_return:
                            self.signals.error.emit(
                                QCoreApplication.translate("LyricProcess",
                                                           "搜索歌词没有任何结果,源:{0}, 歌名:{1}, : {2}").format(
                                                               song_info['source'], song_info['title'], song_info['hash']))
                            continue
                        info = song_info
                        info.update(search_return[0])
                from_cache = self.get_merged_lyric(info, self.task["lyric_type"])
                if not from_cache:  # 检查是否来自缓存
                    time.sleep(1)
        self.is_running = False

    def stop(self) -> None:
        self.is_running = False

    def get_lyrics(self, song_info: dict) -> tuple[None | Lyrics, bool]:
        logging.debug(f"开始获取歌词：{song_info['id']}")
        from_cache = False
        if ("lyrics", song_info["source"], song_info['id'], song_info.get("accesskey", "")) in cache:
            lyrics = cache[("lyrics", song_info["source"], song_info['id'], song_info.get("accesskey", ""))]
            logging.info(f"从缓存中获取歌词：源:{song_info['source']}, id:{song_info['id']}, accesskey: {song_info.get('accesskey', '无')}")
            from_cache = True
        if not from_cache:
            lyrics = Lyrics(song_info)

            for _i in range(3):  # 重试3次
                error1, error1_type = lyrics.download_and_decrypt()
                if error1_type != LyricsProcessingError.REQUEST:  # 如果正常或不是请求错误不重试
                    break

            if error1 is not None:
                return (error1, error1_type), False

            if error1_type != LyricsProcessingError.REQUEST and not from_cache:  # 如果不是请求错误则缓存
                cache[("lyrics", song_info["source"], song_info['id'], song_info.get("accesskey", ""))] = lyrics
        return lyrics, from_cache

    def get_merged_lyric(self, song_info: dict, lyric_type: list) -> bool:
        logging.debug(f"开始获取合并歌词：{song_info.get('id', song_info.get('hash', ''))}")
        lyrics, from_cache = self.get_lyrics(song_info)
        if isinstance(lyrics, tuple):
            song_name_str = QCoreApplication.translate("LyricProcess", "歌名:") + song_info["title"] if "title" in song_info else ""
            logging.error(f"获取歌词失败：{song_name_str}, 源:{song_info['source']}, id: {song_info['id']},错误：{lyrics[0]}")
            self.signals.error.emit(QCoreApplication.translate("LyricProcess", "获取 {0} 加密歌词失败:{1}").format(song_name_str, lyrics[0]))
            return from_cache

        type_mapping = {"原文": "orig", "译文": "ts", "罗马音": "roma"}
        lyrics_order = [type_mapping[type_] for type_ in cfg["lyrics_order"] if type_mapping[type_] in lyric_type]

        try:
            merged_lyric = lyrics.get_merge_lrc(lyrics_order, self.task["lyrics_format"], self.task.get("offset", 0))
        except Exception as e:
            logging.exception("合并歌词失败")
            self.signals.error.emit(QCoreApplication.translate("LyricProcess", "合并歌词失败：{0}").format(str(e)))

        if not self.is_running:
            logging.debug("任务被取消")
            return from_cache

        if self.task["type"] == "get_merged_lyric":
            self.signals.result.emit(self.taskid, {'info': {**song_info, 'lyrics_format': self.task["lyrics_format"]}, 'lrc': lyrics, 'merged_lyric': merged_lyric})

        elif self.task["type"] == "get_list_lyrics":
            save_folder, file_name = get_save_path(self.task["save_folder"],
                                                   self.task["lyrics_file_name_format"] + get_lyrics_format_ext(self.task["lyrics_format"]),
                                                   song_info,
                                                   lyrics_order)
            save_path = os.path.join(save_folder, file_name)  # 获取保存路径
            inst = bool(self.skip_inst_lyrics and len(lyrics["orig"]) != 0 and
                        (lyrics["orig"][0][2][0][2] == "此歌曲为没有填词的纯音乐，请您欣赏" or
                         lyrics["orig"][0][2][0][2] == "纯音乐，请欣赏"))
            self.signals.result.emit(self.count, {'info': song_info, 'save_path': save_path, 'merged_lyric': merged_lyric, 'inst': inst})
        logging.debug("发送结果信号")
        return from_cache


class GetSongListSignal(QObject):
    result = Signal(int, str, list)
    error = Signal(str)


class GetSongListWorker(QRunnable):

    def __init__(self, taskid: int, list_type: str, list_id: str, source: Source) -> None:
        super().__init__()
        self.taskid = taskid
        self.list_type = list_type
        self.id = list_id
        self.source = source
        self.signals = GetSongListSignal()

    def run(self) -> None:
        if ("songlist", self.list_type, self.id) in cache:
            self.signals.result.emit(self.taskid, self.list_type, cache[("songlist", self.list_type, self.id)])
            return
        for _i in range(3):
            match self.source:
                case Source.QM:
                    if self.list_type == "album":
                        song_list = qm_get_album_song_list(self.id)
                    elif self.list_type == "songlist":
                        song_list = qm_get_songlist_song_list(self.id)
                case Source.NE:
                    song_list = ne_get_songlist(self.id, self.list_type)
                case Source.KG:
                    song_list = kg_get_songlist(self.id, self.list_type)
            if isinstance(song_list, list):
                break

        if isinstance(song_list, list) and song_list:
            self.signals.result.emit(self.taskid, self.list_type, song_list)
            cache[("songlist", self.list_type, self.id)] = song_list
        elif song_list == []:
            self.signals.error.emit(QCoreApplication.translate("LyricProcess", "获取歌曲列表失败, 列表数据为空"))
        elif isinstance(song_list, str):
            self.signals.error.emit(QCoreApplication.translate("LyricProcess", "获取歌曲列表失败：{0}").format(song_list))
        else:
            self.signals.error.emit(QCoreApplication.translate("LyricProcess", "获取歌曲列表失败, 未知错误"))


class LocalMatchSignal(QObject):
    massage = Signal(str)
    error = Signal(str, int)
    finished = Signal()
    progress = Signal(int, int)


class LocalMatchWorker(QRunnable):

    def __init__(self,
                 infos: dict,
                 ) -> None:
        super().__init__()
        self.signals = LocalMatchSignal()
        self.song_path: str = infos["song_path"]
        self.save_path: str = infos["save_path"]
        self.save_mode: LocalMatchSaveMode = infos["save_mode"]
        self.fliename_mode: LocalMatchFileNameMode = infos["flienmae_mode"]
        self.lyrics_order: list[str] = infos["lyrics_order"]
        self.lyrics_format: LyricsFormat = infos["lyrics_format"]
        self.source: list[Source] = infos["source"]

        self.is_running = True
        self.current_index = 0
        self.total_index = 0

        self.skip_inst_lyrics = cfg["skip_inst_lyrics"]
        self.file_name_format = cfg["lyrics_file_name_format"]

        self.LyricProcessingWorker = LyricProcessingWorker({"lyrics_format": infos["lyrics_format"]})
        self.LyricProcessingWorker.signals.error.connect(self.lyric_processing_error)

        self.min_score = infos["min_score"]

    def lyric_processing_error(self, error: str) -> None:
        self.signals.error.emit(error, 0)

    def stop(self) -> None:
        self.is_running = False

    def fetch_next_lyrics(self) -> None:
        info = self.song_infos[self.current_index]
        self.current_index += 1
        worker = AutoLyricsFetcher(info, self.min_score, self.source)
        worker.signals.result.connect(self.handle_fetch_result)
        threadpool.start(worker)

    def run(self) -> None:
        self.loop = QEventLoop()
        logging.info(f"开始本地匹配歌词,源:{self.source}")
        try:
            self.start_time = time.time()
            # Setp 1 处理cue 与 遍历歌曲文件
            self.signals.massage.emit(QCoreApplication.translate("LocalMatch", "处理 cue 并 遍历歌曲文件..."))
            song_infos = []
            cue_audio_files = []
            cue_count = 0
            audio_file_paths = []
            for root, _dirs, files in os.walk(self.song_path):
                for file in files:
                    if not self.is_running:
                        self.signals.finished.emit()
                        return
                    if file.lower().endswith('.cue'):
                        file_path = os.path.join(root, file)
                        try:
                            songs, cue_audio_file_paths = parse_cue(file_path)
                            if len(songs) > 0:
                                song_infos.extend(songs)
                                cue_audio_files.extend(cue_audio_file_paths)
                                cue_count += 1
                            else:
                                logging.warning(f"没有在cue文件 {file_path} 解析到歌曲")
                                self.signals.error.emit(QCoreApplication.translate("LocalMatch", "没有在cue文件 {0} 解析到歌曲").format(file_path), 0)
                        except Exception as e:
                            logging.exception("处理cue文件时错误")
                            self.signals.error.emit(f"处理cue文件时错误:{e}", 0)
                    elif file.lower().split(".")[-1] in audio_formats:
                        file_path = os.path.join(root, file)
                        audio_file_paths.append(file_path)

            for cue_audio_file in cue_audio_files:  # 去除cue中有的文件
                if cue_audio_file in audio_file_paths:
                    audio_file_paths.remove(cue_audio_file)
            msg = QCoreApplication.translate("LocalMatch", "共找到{0}首歌曲").format(f"{len(audio_file_paths) + len(song_infos)}")
            if cue_count > 0:
                msg += QCoreApplication.translate("LocalMatch", "，其中{0}首在{1}个cue文件中找到").format(f"{len(song_infos)}", str(cue_count))

            self.signals.massage.emit(msg)

            # Step 2 读取歌曲文件信息
            self.signals.massage.emit(QCoreApplication.translate("LocalMatch", "正在读取歌曲文件信息..."))
            total_paths = len(audio_file_paths)
            for i, audio_file_path in enumerate(audio_file_paths):
                self.signals.progress.emit(i, total_paths)
                if not self.is_running:
                    self.signals.finished.emit()
                    return
                song_info = get_audio_file_info(audio_file_path)
                if isinstance(song_info, str):
                    self.signals.error.emit(song_info, 0)
                    continue
                if isinstance(song_info, dict):
                    song_infos.append(song_info)

            # Step 3 根据信息搜索并获取歌词
            logging.debug(f"song_infos: {json.dumps(song_infos, indent=4, ensure_ascii=False)}")
            self.total_index = len(song_infos)
            self.song_infos: list[dict] = song_infos
            if self.total_index > 0:
                self.signals.massage.emit(QCoreApplication.translate("LocalMatch", "正在搜索并获取歌词..."))
                self.fetch_next_lyrics()
            else:
                self.signals.massage.emit(QCoreApplication.translate("LocalMatch", "没有找到歌曲可查找歌词的歌曲"))
                self.signals.finished.emit()
        except Exception as e:
            logging.exception("搜索歌词时错误")
            self.signals.error.emit(str(e), 1)
        finally:
            self.loop.exec()
            self.signals.finished.emit()

    def handle_fetch_result(self, result: dict[str, dict | Lyrics | str]) -> None:
        try:
            song_info = result["orig_info"]

            simple_song_info_str = f"{song_info['artist']} - {song_info['title']}" if "artist" in song_info else song_info["title"]

            match result['state']:
                case "成功":
                    if self.skip_inst_lyrics is False or result.get("is_inst") is False:
                        # 不是纯音乐或不要跳过纯音乐
                        lrc_info = result["result_info"]
                        lyrics: Lyrics = result["lyrics"]
                        # Step 4 合并歌词
                        merged_lyric = lyrics.get_merge_lrc(self.lyrics_order, self.lyrics_format)
                        # Step 5 保存歌词
                        match self.save_mode:
                            case LocalMatchSaveMode.MIRROR:
                                save_folder = os.path.join(self.save_path,
                                                           os.path.dirname(os.path.relpath(song_info["file_path"], self.song_path)))
                            case LocalMatchSaveMode.SONG:
                                save_folder = os.path.dirname(song_info["file_path"])

                            case LocalMatchSaveMode.SPECIFY:
                                save_folder = self.save_path

                        save_folder = escape_path(save_folder).strip()

                        match self.fliename_mode:
                            case LocalMatchFileNameMode.SONG:
                                if song_info["type"] == "cue":
                                    save_folder = os.path.join(save_folder, os.path.splitext(os.path.basename(song_info["file_path"]))[0])
                                    save_filename = escape_filename(replace_info_placeholders(self.file_name_format, lrc_info, self.lyrics_order)) + get_lyrics_format_ext(self.lyrics_format)
                                else:
                                    save_filename = os.path.splitext(os.path.basename(song_info["file_path"]))[0] + get_lyrics_format_ext(self.lyrics_format)

                            case LocalMatchFileNameMode.FORMAT:
                                save_filename = escape_filename(replace_info_placeholders(self.file_name_format, lrc_info, self.lyrics_order)) + get_lyrics_format_ext(self.lyrics_format)

                        save_path = os.path.join(save_folder, save_filename)
                        try:
                            if not os.path.exists(os.path.dirname(save_path)):
                                os.makedirs(os.path.dirname(save_path))
                            with open(save_path, "w", encoding="utf-8") as f:
                                f.write(merged_lyric)
                            msg = (f"[{self.current_index}/{self.total_index}]" +
                                   QCoreApplication.translate("LocalMatch", "本地") + f": {simple_song_info_str} " +
                                   QCoreApplication.translate("LocalMatch", "匹配") + f": {lrc_info['artist']} - {lrc_info['title']} " +
                                   QCoreApplication.translate("LocalMatch", "成功保存到") + f"{save_path}")
                            self.signals.massage.emit(msg)
                        except Exception as e:
                            self.signals.error.emit(str(e), 0)
                    else:
                        # 是纯音乐并要跳过纯音乐
                        msg = (f"[{self.current_index}/{self.total_index}]" +
                               QCoreApplication.translate("LocalMatch", "本地") + f": {simple_song_info_str} " +
                               QCoreApplication.translate("LocalMatch", "搜索结果") +
                               f":{simple_song_info_str} " + QCoreApplication.translate("LocalMatch", "跳过纯音乐"))
                        self.signals.massage.emit(msg)
                case "没有找到符合要求的歌曲":
                    msg = (f"[{self.current_index}/{self.total_index}] {simple_song_info_str}:" + QCoreApplication.translate("LocalMatch", "没有找到符合要求的歌曲"))
                    self.signals.massage.emit(msg)
                case "搜索结果处理失败":
                    msg = (f"[{self.current_index}/{self.total_index}] {simple_song_info_str}:" + QCoreApplication.translate("LocalMatch", "搜索结果处理失败"))
                    self.signals.massage.emit(msg)
                case "没有足够的信息用于搜索":
                    msg = (f"[{self.current_index}/{self.total_index}] {simple_song_info_str}:" + QCoreApplication.translate("LocalMatch", "没有足够的信息用于搜索"))
                    self.signals.massage.emit(msg)
                case "超时":
                    msg = (f"[{self.current_index}/{self.total_index}] {simple_song_info_str}:" + QCoreApplication.translate("LocalMatch", "超时"))
                    self.signals.massage.emit(msg)

        except Exception:
            logging.exception("合并或保存时出错")
            msg = (f"[{self.current_index}/{self.total_index}] {simple_song_info_str}:" + QCoreApplication.translate("LocalMatch", "合并或保存时出错"))
            self.signals.massage.emit(msg)
        finally:
            self.signals.progress.emit(self.current_index, self.total_index)
            if self.current_index == self.total_index:
                self.signals.massage.emit(QCoreApplication.translate("LocalMatch", "匹配完成,耗时{0}秒").format(f"{time.time() - self.start_time}"))
                self.loop.quit()
            elif self.is_running:
                self.fetch_next_lyrics()
            else:
                self.loop.quit()


class AutoLyricsFetcherSignal(QObject):
    result = Signal(dict)


class AutoLyricsFetcher(QRunnable):

    def __init__(self, info: dict, min_score: float = 60, source: list | None = None) -> None:
        super().__init__()
        # print("--------------------------------------------------------------AutoLyricsFetcher init--------------------------------------------------------------")
        self.info = info
        if source:
            self.source = source
        else:
            self.source = [Source.QM, Source.KG, Source.NE]
        self.signals = AutoLyricsFetcherSignal()
        self.search_task = {}
        self.search_task_finished = 0
        self.get_task = {}
        self.get_task_finished = 0
        self.infos2get = []
        self.errors = []
        self.min_score = min_score
        self.obtained_lyrics: list[tuple[dict, Lyrics]] = []

        self.result = None

    def new_search_work(self, keyword: str, search_type: SearchType, source: Source) -> None:
        task_id = len(self.search_task)
        self.search_task[task_id] = (keyword, search_type, source)
        worker = SearchWorker(task_id, *self.search_task[task_id])
        worker.signals.result.connect(self.handle_search_result, Qt.BlockingQueuedConnection)
        worker.signals.error.connect(self.handle_search_error, Qt.BlockingQueuedConnection)
        threadpool.start(worker)

    def new_get_work(self, song_info: dict) -> None:
        task_id = len(self.get_task)
        self.get_task[task_id] = song_info
        worker = LyricProcessingWorker({"id": task_id, "song_info": song_info, "type": "get_lyric"})
        worker.signals.result.connect(self.handle_get_result, Qt.BlockingQueuedConnection)
        threadpool.start(worker)

    def search(self) -> None:
        artist: str | None = self.info.get('artist')
        keyword = f"{artist} - {self.info['title'].strip()}" if artist and artist.strip() else self.info["title"].strip()
        for source in self.source:
            self.new_search_work(keyword, SearchType.SONG, source)

    @Slot(str)
    def handle_search_error(self, error: str) -> None:
        # 错误
        self.search_task_finished += 1
        self.errors.append(error)
        self.get_result()

    @Slot(int, SearchType, list)
    def handle_search_result(self, taskid: int, search_type: SearchType, infos: list[dict]) -> None:

        def unified_symbol(text: str) -> str:
            text = text.strip().replace('（', '(').replace('）', ')').replace('：', ':')
            return re.sub(r"\s", " ", text)

        def calculate_artist_score(artist1: str, artist2: str) -> float:
            artist1, artist2 = unified_symbol(artist1), unified_symbol(artist2)
            if not artist1 or not artist2:
                return -1
            if artist1.lower() == artist2.lower():
                return 100

            score = max(text_difference(artist1.lower(), artist2.lower()), 0) * 100  # 计算文本相似度得到的分数

            aliases1: list[str] = re.findall(r"\([Cc][Vv][.:]?\s?([^\)]*)\)", artist1)  # 获取别名
            aliases2: list[str] = re.findall(r"\([Cc][Vv][.:]?\s?([^\)]*)\)", artist2)  # 获取别名

            if " " in artist1 and artist1.split(" ")[0] == artist2 and len(artist2) > 3 and score < 80:
                return 80

            if len(aliases1) == len(aliases2):
                sub_score = 0
                for alias1 in aliases1:
                    if alias1.strip() in aliases2:
                        sub_score += 100 / len(aliases1)
                if sub_score > score:
                    return sub_score

            elif aliases1:
                sub_score = 0
                for alias1 in aliases1:
                    if alias1.strip() in artist2:
                        sub_score += 100 / len(aliases1)
                if sub_score > score:
                    return sub_score

            elif aliases2:
                sub_score = 0
                for alias2 in aliases2:
                    if alias2.strip() in artist1:
                        sub_score += 100 / len(aliases2)
                    if sub_score > score:
                        return sub_score

            return score

        def calculate_title_score(title1: str, title2: str) -> float:
            def get_tags(not_same: str) -> tuple[list, str]:
                not_same_tags: list[tuple[str, str, str, str]] = re.findall(r"[-<(\[～]([～\]^)>-]*)[～\]^)>-]|(\w+ ?(?:(?:solo |size )?ver(?:sion)?\.?|size|style|mix(?:ed)?|edit(?:ed)?|版|solo))|(纯音乐|inst\.?(?:rumental)|off ?vocal(?: ?[Vv]er.)?)", not_same)
                not_same_tags: list[str] = [item.strip() for tup in not_same_tags for item in tup if item]  # 去除空字符串与符号
                not_same_other = re.sub(r"|".join(not_same_tags) + r"|[-><)(\]\[～]", "", not_same)  # 获取非tags部分

                # 统一一些tags
                for i in range(len(not_same_tags)):
                    tag = not_same_tags[i]
                    tag = re.sub(r"ver(?:sion)?\.?", "ver", tag)
                    tag = re.sub(r"伴奏|纯音乐|inst\.?(?:rumental)|off ?vocal(?: ?[Vv]er.)?", "inst", tag)
                    tag = tag.replace("mixed", "mix").replace("edited", "edit")
                    tag = re.sub(r"(solo|mix|edit|style|size) ver", r"\1", tag)
                    tag = re.sub("(?:tv|anime) ?(?:サイズ|size)?(?: ?ver)?", "tv size", tag)
                    not_same_tags[i] = tag

                return not_same_tags, not_same_other

            title1, title2 = unified_symbol(title1).lower(), unified_symbol(title2).lower()
            if title1 == title2:
                return 100

            score0 = max(text_difference(title1, title2), 0) * 100  # 计算文本相似度得到的分数
            same_begin = ""  # 开头相同的字符串

            for i in range(len(title1)):
                if len(title2) > i and title1[i] == title2[i]:
                    same_begin += title1[i]
                else:
                    break

            if same_begin in (title1, title2) or not same_begin:
                return score0

            not_same1_tags, not_same1_other = get_tags(title1[len(same_begin):])
            not_same2_tags, not_same2_other = get_tags(title2[len(same_begin):])

            # 计算tags相似度
            tag1_no_match = []
            tag2_no_match = not_same2_tags
            for tag1 in not_same1_tags:
                if tag1 in not_same2_tags:
                    # tag匹配
                    tag2_no_match.remove(tag1)
                elif re.findall(r"(?:solo|mix|edit|style|size|edit|inst)$", tag1):
                    # 普通标签
                    tag1_no_match.append(tag1)
                elif tag1 in not_same2_other:
                    not_same1_other += tag1

            for tag2 in tag2_no_match:
                if not re.findall(r"(?:solo|mix|edit|style|size|edit|inst)$", tag2) and tag2 in not_same1_other:
                    not_same2_other += tag2
                    tag2_no_match.remove(tag2)

            kp = len(same_begin) / ((len(not_same1_other) + len(not_same2_other)) / 2 + len(same_begin))
            score1 = 100 * kp + max(text_difference(not_same1_other, not_same2_other), 0) * (1 - kp)

            if not tag1_no_match and not tag2_no_match:
                return max(score1 * 0.7 + 30, score0)

            score2, score3 = 0, 0
            if tag1_no_match and tag2_no_match:
                for tag1 in tag1_no_match:
                    score2 += max([text_difference(tag1, tag2) for tag2 in tag2_no_match]) * (30 / len(tag1_no_match))

                for tag2 in tag2_no_match:
                    score3 += max([text_difference(tag1, tag2) for tag1 in tag1_no_match]) * (30 / len(tag2_no_match))

            return max(score1 * 0.7 + max(score2, score3), score0)

        try:
            self.search_task_finished += 1
            keyword, _search_type, source = self.search_task[taskid]
            if source == Source.KG and search_type == SearchType.LYRICS:
                self.new_get_work(infos[0])
            duration: int | None = self.info.get('duration')
            artist: str | None = self.info.get('artist')
            if artist and artist.strip():
                artist = artist.strip()

            score_info: list[tuple[float, dict]] = []
            for info in infos:
                score = 0
                if duration and abs(info.get('duration', -100) - duration) > 3:
                    continue

                if artist:
                    artist_score = calculate_artist_score(artist, info.get('artist', ''))
                    if artist_score != -1:
                        score += artist_score * 0.5

                if score == 0:
                    score += calculate_title_score(self.info['title'], info.get('title', ''))
                else:
                    score += calculate_title_score(self.info['title'], info.get('title', '')) * 0.5

                if score > self.min_score:
                    # print(f"{self.info['title']}\t{artist}|{info.get('title', '')}\t{info.get('artist', '')}|{artist_score}\t{(score - artist_score * 0.5) * 2}\t{score}")
                    score_info.append((score, info))

            score_info = sorted(score_info, key=lambda x: x[0], reverse=True)
            best: dict = score_info[0] if score_info else None
            if best:
                best_info = best[1]
                best_info['score'] = best[0]
                logging.info(f"关键词: {keyword} 搜索结果: {best}")
                if source == Source.KG and search_type == SearchType.SONG:
                    #  对酷狗音乐搜索到的歌曲搜索歌词
                    self.new_search_work(best_info, SearchType.LYRICS, Source.KG)
                else:
                    self.new_get_work(best_info)
            elif keyword == f"{artist} - {self.info['title'].strip()}":
                # 尝试只搜索歌曲名
                self.new_search_work(self.info['title'].strip(), SearchType.SONG, source)
            else:
                logging.warning(f"无法从源:{source}找到符合要求的歌曲:{self.info}")
        except Exception:
            logging.exception("搜索结果处理失败")
            self.send_result({"state": "搜索结果处理失败", "orig_info": self.info})
        finally:
            self.get_result()

    def handle_get_result(self, task_id: int, result: list) -> None:
        try:
            self.get_task_finished += 1
            if "error_type" in result:
                if result["error_type"] != LyricsProcessingError.NOT_FOUND:
                    self.errors.append(result["error_str"])
            else:
                song_info: dict = self.get_task[task_id]
                result: Lyrics = result["result"]
                self.obtained_lyrics.append((song_info, result))
            self.get_result()
        except Exception:
            logging.exception("歌词结果处理失败")
            self.send_result({"state": "歌词结果处理失败", "orig_info": self.info})

    def get_result(self) -> None:

        if self.search_task_finished == len(self.search_task) != 0 and not self.get_task:
            # 没有任何符合要求的歌曲
            logging.warning(f"没有找到符合要求的歌曲:{self.info}")
            self.send_result({"state": "没有找到符合要求的歌曲", "orig_info": self.info})
            return

        if (self.search_task_finished != len(self.search_task) or
           len(self.get_task) != self.get_task_finished or
           len(self.get_task) == 0 or
           self.get_task_finished == 0):
            self.loop.processEvents()
            return

        if len(self.obtained_lyrics) == 0:
            self.send_result({"state": "没有找到符合要求的歌曲", "orig_info": self.info})
            return

        # 去除相似度低的
        highest_score = 0
        for obtained_lyric in self.obtained_lyrics:
            highest_score = max(obtained_lyric[0]['score'], highest_score)

        for obtained_lyric in self.obtained_lyrics:
            if abs(obtained_lyric[0]['score'] - highest_score) > 15:
                self.obtained_lyrics.remove(obtained_lyric)

        have_verbatim = [lyrics for lyrics in self.obtained_lyrics if lyrics[1].lrc_isverbatim.get("orig") is True]
        have_ts = [lyrics for lyrics in self.obtained_lyrics if "ts" in lyrics[1].lrc_types]
        have_roma = [lyrics for lyrics in self.obtained_lyrics if "roma" in lyrics[1].lrc_types]

        have_verbatim_ts: list[tuple[dict, Lyrics]] = []
        have_verbatim_roma: list[tuple[dict, Lyrics]] = []
        have_verbatim_ts_roma: list[tuple[dict, Lyrics]] = []
        have_ts_roma: list[tuple[dict, Lyrics]] = []
        for lyrics in self.obtained_lyrics:
            if lyrics in have_verbatim and lyrics in have_ts:
                have_verbatim_ts.append(lyrics)
            if lyrics in have_verbatim and lyrics in have_roma:
                have_verbatim_roma.append(lyrics)
            if lyrics in have_verbatim and lyrics in have_ts and lyrics in have_roma:
                have_verbatim_ts_roma.append(lyrics)
            if lyrics in have_ts and lyrics in have_roma:
                have_ts_roma.append(lyrics)

        for lyrics_list in [have_verbatim_ts_roma, have_verbatim_ts, have_ts_roma, have_ts, have_verbatim_roma, have_verbatim, have_roma]:
            if lyrics_list:
                break
        else:
            lyrics_list = self.obtained_lyrics

        for source in self.source:
            for lyrics in lyrics_list:
                if lyrics[0]["source"] == source:
                    info = lyrics[0]
                    result = lyrics[1]
                    break
            else:
                continue
            break
        else:
            logging.warning(f"没有找到符合要求的歌曲:{self.info}")
            self.send_result({"state": "没有找到符合要求的歌曲", "orig_info": self.info})
            return

        # 判断是否为纯音乐
        if ((info["source"] == Source.KG and info['language'] in ["纯音乐", '伴奏']) or
            (len(result["orig"]) > 0 and len(result["orig"][0][2]) > 0 and
            (result["orig"][0][2][0][2] == "此歌曲为没有填词的纯音乐，请您欣赏" or
             result["orig"][0][2][0][2] == "纯音乐，请欣赏")) or
                re.findall(r"伴奏|纯音乐|inst\.?(?:rumental)|off ?vocal(?: ?[Vv]er.)?", info.get('title', '') + self.info['title'])):
            is_inst = True
        else:
            is_inst = False

        self.send_result({"state": "成功", "orig_info": self.info, "lyrics": result, "is_inst": is_inst, "result_info": info})

    def send_result(self, result: dict) -> None:
        self.result = result
        self.loop.exit()

    def run(self) -> None:
        self.loop = QEventLoop()
        if not self.info.get("title") or not self.info["title"].strip():
            self.send_result({"state": "没有足够的信息用于搜索", "orig_info": self.info})
            return
        self.search()
        self.timer = QTimer()
        self.timer.start(30 * 1000)
        self.timer.timeout.connect(lambda: self.send_result({"state": "超时", "orig_info": self.info}))
        self.loop.exec()
        self.timer.stop()
        self.signals.result.emit(self.result)  # 最后发送结果[不然容易出错(闪退、无响应)]
