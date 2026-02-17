import os,logging,re,asyncio
from pipelines.telegram_send import send_source_video,send_video,send_top5
from spiders.hanime1.crawler import Hanime1spider
from pipelines.aria2_download import start_batch_download
from pipelines.load import load_yaml
from spiders.base_spider import CrawlResult
from utils.pic_utils import generate_thumbnail
from datetime import datetime, timezone, timedelta
from pipelines.data_base import DataBase

logger = logging.getLogger(__name__)

base_path = os.getcwd()
spider_path = os.path.join(base_path, "download", "hanime1")
video_path = os.path.join(spider_path, "video")
cover_path = os.path.join(spider_path, "cover")
preview_path = os.path.join(spider_path, "preview")



async def do_hanime1(client,db:DataBase):
    try:
        cfg = load_yaml('hanime1.yaml')
        video_ch = cfg['video_channel']
        pic_ch = cfg['pic_channel']
        vid_name = re.sub(r'^@', '', video_ch)
        hm = Hanime1spider(cfg,db)
        spider:CrawlResult = await hm.do_job()
        if not spider.success:
            logger.warning('未能正确爬取')
            return False
        send_semaphore = asyncio.Semaphore(1)
        data_list = spider.data
        name_list = [item[0] for item in data_list]
        source_url_list = [item[1] for item in data_list]
        id_lists = []
        ch_ids = []
        date = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        for url in source_url_list:
            match = re.search(r"\?v=(\d+)", url)
            if match:
                id_lists.append(int(match.group(1)))
        for i in range(0, 30, 5):
            na_list = name_list[i:i+5]
            url_list = source_url_list[i:i+5]
            id_list = id_lists[i:i+5]
            ch_ids.clear()
            down_url_list = spider.detail[i:i+5]

            video_paths =  await start_batch_download(down_url_list,video_path,na_list)
            mini_thumbs = [os.path.join(cover_path, f"{vid_id}_thumb.jpg")for vid_id in id_list]

            pic_list = [generate_thumbnail(t_video_path = vi_paths,thumb_path=preview_path,cover_path= cover_path,vid_id=vid,num= top,today=date,clean_name=na_ls)for vi_paths,vid,top,na_ls in zip(video_paths,id_list,range(30-i,25-i,-1),na_list)]
            pic_task =  asyncio.gather(*pic_list)

            for ti, vid_path, mini_path, v_id in zip(na_list, video_paths, mini_thumbs, id_list):
                if vid_path == 0:
                    # 视频已存在，从数据库获取之前保存的频道消息 ID
                    info = await db.get_hanime1_info(v_id)
                    ch_id = info[1] if info != 0 else 0
                else:
                    # 视频是新下载的，上传并保存
                    ch_id = await send_source_video(client=client, title=ti, path=vid_path, ch_id=video_ch,mini_thumb_path=mini_path, semaphore=send_semaphore)
                    await db.insert_hanime1_info(v_id, ti, ch_id)

                ch_ids.append(ch_id)

            date = datetime.now(timezone(timedelta(hours=8))).date().isoformat()

            await asyncio.gather(pic_task)
            prv_path = [os.path.join(preview_path, f"{video_id}.jpg") for video_id in id_list]
            for vid,urls,num,prv_pa,tit,ch__id in zip(id_list,url_list,range(30-i,25-i,-1),prv_path,na_list,ch_ids):
                await send_video(client=client,video_id=vid,url=urls,top = num,path=prv_pa,channel_id=pic_ch,title=tit,ch_name=vid_name,ch_id=ch__id)

        rank_list = [f'https://t.me/{vid_name}/{video_id}'for video_id in ch_ids][::-1]
        cover_paths = [os.path.join(cover_path,f"{video_id}.jpg")for video_id in id_lists[-5:]][::-1]
        await send_top5(client,ch_id=pic_ch,ranks=rank_list,source='hanime1',paths=cover_paths)
    except Exception as e:
        logger.error(e,exc_info=True)


