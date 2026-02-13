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
        ch_name = re.sub(r'^@', '', pic_ch)
        hm = Hanime1spider(cfg)
        spider:CrawlResult = await hm.do_job()
        if not spider.success:
            logger.warning('未能正确爬取')
            return False
        data_list = spider.data
        name_list = [item[0] for item in data_list]
        source_url_list = [item[1] for item in data_list]
        id_lists = []
        for url in source_url_list:
            match = re.search(r"\?v=(\d+)", url)
            if match:
                id_lists.append(int(match.group(1)))
        for i in range(0, 30, 5):
            na_list = name_list[i:i+5]
            url_list = source_url_list[i:i+5]
            id_list = id_lists[i:i+5]
            need_down_list = await if_exit(id_list,db)
            down_url_list = [val_b if val_a != 0 else 0 for val_a, val_b in zip(need_down_list, spider.detail[i:i+5])]

            video_paths =  await start_batch_download(down_url_list,video_path,na_list)

            send_source_list = [send_source_video(client=client,title= ti,path= vid_path,ch_id = video_ch)for ti,vid_path in zip(id_list,video_paths)]
            send_source_task= asyncio.gather(*send_source_list)

            date = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
            pic_list = [generate_thumbnail(t_video_path = vi_paths,thumb_path=preview_path,cover_path= cover_path,vid_id=vid,num= top,today=date,clean_name=na_list)for vi_paths,vid,top in zip(video_paths,id_list,range(30-i,24-i,-1))]
            pic_task =  asyncio.gather(*pic_list)

            task_list = [send_source_task,pic_task]
            await asyncio.gather(*task_list)
            prv_path =[os.path.join(preview_path,video_id)for video_id in id_list]
            send_pre_task = [send_video(client=client,video_id=vid,url=urls,top = num,path=prv_path,channel_id=video_ch,title=tit,ch_name=ch_name)for vid,urls,num,tit in zip(id_list,url_list,range(30-i,24-i,-1),na_list)]
            await asyncio.gather(*send_pre_task)

        rank_list = [f'https://t.me/{ch_name}/{video_id}'for video_id in id_lists[25:29]][::-1]
        cover_paths = [os.path.join(preview_path,video_id)for video_id in id_lists[25:29]][::-1]
        await send_top5(client,ch_id=pic_ch,ranks=rank_list,source='hanime1',paths=cover_paths)
    except Exception as e:
        logger.error(e)


async def if_exit(id_list,db:DataBase):
    download_lists = []
    find_task = [db.get_hanime1_info(vid_id) for vid_id in id_list]
    find = await asyncio.gather(*find_task)
    for i,vid_id in enumerate(find):
        if vid_id == 0: #不存在,原样
            download_lists.append(id_list[i])
        else:       #存在,置0
            download_lists.append(0)
    return download_lists


