const fs = require('fs');

// ===================== 配置 =====================
const DAYS_LIMIT = 14; // 只保留14天内的岗位
// ===============================================

const inputFile = process.argv[2];
const outputFile = process.argv[3];

if (!inputFile || !outputFile) {
  console.error("❌ 用法：node parse-jobs.js 输入文件.json 输出文件.json");
  process.exit(1);
}

console.log(`✅ 开始处理：输入=${inputFile}，输出=${outputFile}`);

// 读取 JSON 文件
let jobs = [];
try {
  const rawData = fs.readFileSync(inputFile, 'utf8');
  const data = JSON.parse(rawData);
  
  // 支持两种格式：直接数组 或 { jobs: [...] }
  if (Array.isArray(data)) {
    jobs = data;
  } else if (data.jobs && Array.isArray(data.jobs)) {
    jobs = data.jobs;
  } else {
    console.error("❌ 无法识别的 JSON 格式");
    process.exit(1);
  }
} catch (err) {
  console.error(`❌ 读取文件失败：${err.message}`);
  process.exit(1);
}

console.log(`📊 输入文件包含 ${jobs.length} 个岗位`);

const nowUtc = new Date();
let totalJobs = jobs.length;
let filteredJobs = 0;
const outputJobs = [];

// 处理每个岗位
for (const job of jobs) {
  // ── 提取基础字段 ──────────────────────────────
  const jobId = job.id || 'Unknown';
  const jobTitle = job.title || 'Untitled';
  const company = job.company || job.advertiser?.description || 'Unknown';
  
  // ── 时间处理 ──────────────────────────────────
  let listedAtUtc = job.listing_date || job.listedAt?.dateTimeUtc || job.postedDate;
  
  if (!listedAtUtc) {
    console.log(`⚠️ 跳过：${jobTitle} 无发布时间`);
    continue;
  }
  
  const listedTime = new Date(listedAtUtc);
  if (!Number.isFinite(listedTime.valueOf())) {
    console.log(`⚠️ 跳过：${jobTitle} 时间格式错误：${listedAtUtc}`);
    continue;
  }
  
  const diffDays = (nowUtc - listedTime) / (1000 * 60 * 60 * 24);
  
  // ── 时间筛选 ──────────────────────────────────
  if (diffDays > DAYS_LIMIT) {
    console.log(`⏩ 跳过：${jobTitle} 距今 ${diffDays.toFixed(1)} 天，超过 ${DAYS_LIMIT} 天限制`);
    continue;
  }
  
  // ── 提取 JD 内容 ──────────────────────────────
  const content = job.jd || job.teaser || job.description || '';
  
  // 提取职责
  let responsibilities = [];
  const resMatch = content.match(/(?:Responsibilities|Key Responsibilities|JOB RESPONSIBILITIES)[\s:]*([\s\S]*?)(?=Requirements|Qualifications|JOB REQUIREMENTS|Benefits|$)/i);
  if (resMatch) {
    responsibilities = resMatch[1]
      .split(/[\r\n]+/)
      .map(r => r.replace(/^[\s*•\-]?\s*/, '').trim())
      .filter(r => r.length > 5);
  }
  
  // 提取要求
  let requirements = [];
  const reqMatch = content.match(/(?:Requirements|Qualifications|JOB REQUIREMENTS)[\s:]*([\s\S]*?)(?=Benefits|福利待遇|Interested parties|$)/i);
  if (reqMatch) {
    requirements = reqMatch[1]
      .split(/[\r\n]+/)
      .map(r => r.replace(/^[\s*•\-]?\s*/, '').trim())
      .filter(r => r.length > 5);
  }
  
  // 提取福利
  let benefits = [];
  const benMatch = content.match(/(?:Benefits|福利待遇|We offer)[\s:]*([\s\S]*?)(?=Interested parties|Data received|$)/i);
  if (benMatch) {
    benefits = benMatch[1]
      .split(/[\r\n]+/)
      .map(b => b.replace(/^[\s*•\-]?\s*/, '').trim())
      .filter(b => b.length > 3);
  }
  
  // 兜底：如果都没提取到，用 bullet_points
  if (responsibilities.length === 0 && requirements.length === 0 && benefits.length === 0) {
    if (job.bullet_points && Array.isArray(job.bullet_points)) {
      responsibilities = job.bullet_points;
    } else if (content.length > 50) {
      responsibilities = content
        .split(/[\r\n]+/)
        .map(s => s.trim())
        .filter(s => s.length > 10);
    }
  }
  
  // ── 其他字段 ──────────────────────────────────
  const location = job.location || 'Unknown';
  const workType = Array.isArray(job.work_types) 
    ? job.work_types.join(', ') 
    : (job.workTypes || 'Unknown');
  const salaryLabel = job.salary || 'Not specified';
  
  // ── 组装输出 ──────────────────────────────────
  outputJobs.push({
    jobID: jobId,
    jobTitle: jobTitle,
    company: company,
    location: location,
    workType: workType,
    responsibilities: responsibilities.slice(0, 30),
    requirements: requirements.slice(0, 30),
    benefits: benefits.slice(0, 20),
    isVerified: job.is_featured || job.isVerified || false,
    listedAt: job.listing_date ? new Date(job.listing_date).toLocaleDateString() : '',
    listedAtUtc: listedAtUtc,
    daysDiff: Math.round(diffDays * 10) / 10,
    expiresAt: job.expiresAt || '',
    shareLink: job.link || '',
    salary: salaryLabel,
  });
  
  filteredJobs++;
  console.log(`✅ ${jobTitle} 保留（${diffDays.toFixed(1)}天前）`);
}

// ── 写入输出文件 ──────────────────────────────────
try {
  fs.writeFileSync(outputFile, JSON.stringify(outputJobs, null, 2), 'utf8');
  console.log(`\n✅ 处理完成！`);
  console.log(`   📊 输入：${totalJobs} 个岗位`);
  console.log(`   ✅ 保留：${filteredJobs} 个（14天内）`);
  console.log(`   📁 输出：${outputFile}`);
} catch (err) {
  console.error(`❌ 写入文件失败：${err.message}`);
}