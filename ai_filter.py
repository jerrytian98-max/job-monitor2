import json
import logging
import requests

logger = logging.getLogger(__name__)

def filter_jobs_with_ai(jobs, api_key, prompt, model="gemini-3.5-flash-lite"):
    if not api_key or not prompt or not jobs:
        return jobs
        
    logger.info(f"开始使用 AI 智能过滤 {len(jobs)} 个职位...")
    
    # 构造传递给 AI 的职位数据
    job_data = []
    for i, j in enumerate(jobs):
        job_data.append({
            "id": i,
            "title": j.get("title", ""),
            "company": j.get("company", ""),
            "salary": j.get("salary", "")
        })
        
    # 构建 Gemini API 请求
    model = model or "gemini-3.5-flash-lite"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {
        'Content-Type': 'application/json',
        'x-goog-api-key': api_key,
    }
    
    system_instruction = f'''
你是一个专业的HR和求职助手。用户的求职筛选条件是："{prompt}"
请严格根据用户的条件，评估以下职位列表。
你必须返回一个JSON数组，包含符合条件的职位ID。如果都不符合，返回空数组 []。
不要返回任何其他格式、解释或 markdown 标记，只返回纯JSON数组，例如 [0, 2, 5]。

职位列表：
{json.dumps(job_data, ensure_ascii=False)}
'''

    payload = {"contents": [{"parts": [{"text": system_instruction}]}]}
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        result = response.json()
        
        text = result['candidates'][0]['content']['parts'][0]['text']
        text = text.replace('```json', '').replace('```', '').strip()
        
        valid_ids = json.loads(text)
        if not isinstance(valid_ids, list):
            valid_ids = []

        # 只接受不重复的非负整数，避免布尔值和负索引误选职位。
        safe_ids = []
        for item in valid_ids:
            if type(item) is int and 0 <= item < len(jobs) and item not in safe_ids:
                safe_ids.append(item)

        filtered_jobs = [jobs[i] for i in safe_ids]
        logger.info(f"AI 过滤完成：从 {len(jobs)} 个职位中筛选出 {len(filtered_jobs)} 个。")
        return filtered_jobs
        
    except Exception as e:
        logger.error(f"AI 过滤失败: {e}")
        return jobs
