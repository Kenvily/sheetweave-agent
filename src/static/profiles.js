(() => {
  'use strict';
  const data = JSON.parse(document.getElementById('adaptation-data').textContent);
  const form = document.getElementById('profile-editor'), token = form.dataset.token;
  const byId = id => document.getElementById(id);
  if (data.ai_task && !data.ai_applied) {
    (async () => {
      try {
        for (;;) {
          const response = await fetch(`/profiles/drafts/${token}/status`, {cache:'no-store'});
          const state = await response.json(); if (!response.ok) throw Error(state.error);
          if (state.state === 'ready') { location.reload(); return; }
          byId('analysis-status').textContent = state.state === 'queued'
            ? `方案识别排队中（第 ${state.queue_position || 1} 位）…`
            : '正在识别字段用途、报告目录和文档格式…';
          await new Promise(resolve => setTimeout(resolve, 750));
        }
      } catch (error) { byId('analysis-status').textContent = `${error.message}，刷新可继续查询。`; }
    })();
    return;
  }
  const base = structuredClone(data.rule);
  const labels = {project:'项目 / 分组', detail:'具体事项', content:'主要正文', progress:'按所选周读取（兼容旧方案）',
    status:'状态', owner:'负责人', issues:'问题与风险', next_plan:'后续计划'};
  const headingLabels = ['一级标题','二级标题','三级标题'];
  const numberingChoices = JSON.parse(byId('numbering-options').textContent);
  const rows = [], groups = [], styleRows = [], numberingRows = [];
  const defaults = {
    title:{font:'Microsoft YaHei',size_pt:16,bold:true,line_spacing:1.5},
    heading_1:{font:'Microsoft YaHei',size_pt:16,bold:true,line_spacing:1.5},
    heading_2:{font:'Microsoft YaHei',size_pt:15,bold:true,line_spacing:1.5},
    heading_3:{font:'Microsoft YaHei',size_pt:14,bold:true,line_spacing:1.5},
    body_1:{font:'SimSun',size_pt:10.5,bold:false,line_spacing:1.5}
  };
  const input = (label, value, type='text') => {
    const node=document.createElement('input'); node.type=type; node.setAttribute('aria-label',label);
    if(type==='checkbox') node.checked=Boolean(value); else node.value=value;
    return node;
  };
  const field = (label, control) => {
    const node=document.createElement('label'); node.className='field';
    const span=document.createElement('span'); span.textContent=label; node.append(span,control); return node;
  };
  const originalColumn = key => base.input.column_aliases[key]?.[0];
  const weekColumns=data.table.columns.filter(column=>column.weekly_period);
  const primaryWeek=weekColumns.at(-1)?.name;
  const reservedFields = new Set(Object.keys(base.input.column_aliases));
  for(const column of data.table.columns) {
    const assigned=Object.keys(base.input.column_aliases).find(key=>base.input.column_aliases[key].includes(column.name));
    const select=document.createElement('select'); select.setAttribute('aria-label',`${column.name} 用途`);
    let custom=assigned && !Object.hasOwn(labels,assigned) ? assigned : `field_${column.column}`;
    if (!assigned || Object.hasOwn(labels,assigned)) {
      while(reservedFields.has(custom)) custom += '_extra';
      reservedFields.add(custom);
    }
    for(const [key,label] of [...Object.entries(labels).filter(([key])=>key!=='progress'),[custom,`其他内容 ${column.column}`],['','不使用']]) select.append(new Option(label,key));
    select.value=assigned==='progress'?'content':assigned || (column.name===primaryWeek&&!base.input.column_aliases.content?'content':custom);
    const tr=document.createElement('tr'), th=document.createElement('th'), td=document.createElement('td');
    th.scope='row'; th.textContent=column.name; td.append(select); tr.append(th,td); byId('mapping-rows').append(tr);
    rows.push({column,select,custom});
    select.addEventListener('change',()=>{
      if(select.value==='content')for(const row of rows){
        if(row.select!==select&&row.select.value==='content'){
          row.select.value=row.custom;
          for(const group of groups)if(group.sources.has(row.column.name))group.sources.add(column.name);
        }
      }
      renderGroups();renderStatuses();updateSave();
    });
  }
  const assignments = () => Object.fromEntries(rows.filter(row=>row.select.value).map(row=>[row.column.name,row.select.value]));
  function makeGroup(section) {
    return { headingFields:(section.heading_fields || []).map(key=>key?originalColumn(key):''), path:[...(section.heading_path?.length ? section.heading_path : [section.title])],
      sources:new Set((section.source_fields?.length ? section.source_fields : [section.source_field]).map(key=>key==='progress'?(originalColumn('content') || originalColumn('progress') || primaryWeek):originalColumn(key)).filter(Boolean)),
      unmatchedOnly:Boolean(section.unmatched_only),acceptUnmatched:Boolean(section.accept_unmatched),match:structuredClone(section.match || {})};
  }
  base.sections.forEach(section=>groups.push(makeGroup(section)));
  function renderGroups() {
    byId('outline-groups').replaceChildren();
    const assigned=assignments();
    const mappedLabel=row=>labels[row.select.value] || `其他内容 ${row.column.column}`;
    groups.forEach((group,index)=>{
      for(const key of Object.keys(group.match))if(!Object.values(assigned).includes(key))delete group.match[key];
      const box=document.createElement('article'); box.className='outline-group';
      const header=document.createElement('header');header.className='outline-group-header';
      const title=document.createElement('h4');title.textContent=`目录规则 ${String(index+1).padStart(2,'0')}`;
      const badge=document.createElement('span');badge.className='outline-kind';
      badge.textContent='待归类';
      header.append(title);if(group.unmatchedOnly)header.append(badge);box.append(header);
      const path=document.createElement('div'); path.className='outline-path';
      for(let level=0;level<3;level++) {
        const node=input(`目录 ${index+1} ${headingLabels[level]}`,group.path[level] || '');
        const source=document.createElement('select');
        source.setAttribute('aria-label',`规则 ${index+1} ${headingLabels[level]}来源`);
        source.append(new Option('固定文字',''));
        for(const name of Object.keys(assigned)) source.append(new Option(`取自字段：${name}`,name));
        source.value=group.headingFields[level] || '';
        const sync=()=>{node.hidden=Boolean(source.value);node.required=level===0&&!source.value;};
        source.addEventListener('change',()=>{
          group.headingFields[level]=source.value;
          if(source.value)group.path[level]=source.value;
          else group.path[level]=node.value;
          sync();renderNewProjectTarget();updateSave();
        });
        node.placeholder=level ? '可留空，不生成此层标题' : '填写固定标题';
        node.addEventListener('input',()=>{group.path[level]=node.value;renderNewProjectTarget();});
        const controls=document.createElement('div');controls.append(source,node);sync();
        path.append(field(headingLabels[level],controls));
      }
      box.append(path);
      const bodyFields=document.createElement('fieldset');bodyFields.className='body-fieldset';
      const sourceLabel=document.createElement('legend');sourceLabel.textContent='正文字段 · 可多选';bodyFields.append(sourceLabel);
      const sources=document.createElement('div'); sources.className='source-options';
      const choices=Object.keys(assigned).filter(name=>assigned[name]!=='project');
      for(const name of choices) {
        const title=mappedLabel(rows.find(row=>row.column.name===name));
        const check=input(`目录 ${index+1} 使用 ${title}`,group.sources.has(name),'checkbox');
        check.addEventListener('change',()=>check.checked?group.sources.add(name):group.sources.delete(name));
        const label=document.createElement('label'); label.append(check,document.createTextNode(title)); sources.append(label);
      }
      bodyFields.append(sources);
      box.append(bodyFields);
      const matches=document.createElement('div'); matches.className='match-options';
      const candidates=rows.filter(row=>Object.hasOwn(group.match,row.select.value)&&row.select.value);
      for(const row of candidates) {
        const name=row.column.name, key=row.select.value, options=document.createElement('fieldset');
        options.className='match-values';
        const legend=document.createElement('legend');legend.textContent=`归入此目录的${name}（可多选）`;
        options.append(legend);
        const selected=new Set(group.match[key] || []), checks=[];
        const all=input(`目录 ${index+1} 不限制${name}`,selected.size===0,'checkbox');
        const allLabel=document.createElement('label');allLabel.append(all,document.createTextNode('全部取值（含新增）'));options.append(allLabel);
        const values=[...new Set([...(row.column.values || []),...selected])];
        const sync=()=>{
          all.checked=selected.size===0;
          for(const {value,check} of checks)check.checked=selected.has(value);
          if(selected.size)group.match[key]=[...selected];else delete group.match[key];
          updateSave();
        };
        for(const value of values){
          const check=input(`目录 ${index+1} 匹配${name}：${value}`,selected.has(value),'checkbox');
          const label=document.createElement('label');label.append(check,document.createTextNode(value));options.append(label);
          checks.push({value,check});
          check.addEventListener('change',()=>{if(check.checked)selected.add(value);else selected.delete(value);sync();});
        }
        all.addEventListener('change',()=>{selected.clear();sync();});
        const remove=document.createElement('button');remove.type='button';remove.className='secondary';remove.textContent='删除条件';
        remove.setAttribute('aria-label',`目录 ${index+1} 删除${name}筛选`);
        remove.addEventListener('click',()=>{delete group.match[key];renderGroups();updateSave();});
        options.append(remove);matches.append(options);
      }
      const filter=document.createElement('details'), filterTitle=document.createElement('summary');filter.className='row-filter';
      filterTitle.textContent='只处理指定行（可选）';
      filter.open=Object.keys(group.match).length>0;
      const fieldSelect=document.createElement('select');fieldSelect.setAttribute('aria-label',`目录 ${index+1} 添加筛选字段`);
      fieldSelect.append(new Option('选择 Excel 字段',''));
      for(const row of rows.filter(row=>row.select.value&&!Object.hasOwn(group.match,row.select.value)))fieldSelect.append(new Option(row.column.name,row.select.value));
      const addCondition=document.createElement('button');addCondition.type='button';addCondition.className='secondary';addCondition.textContent='添加条件';
      addCondition.disabled=true;fieldSelect.addEventListener('change',()=>addCondition.disabled=!fieldSelect.value);
      addCondition.addEventListener('click',()=>{group.match[fieldSelect.value]=[];renderGroups();updateSave();});
      const filterActions=document.createElement('div');filterActions.className='profile-actions';filterActions.append(fieldSelect,addCondition);
      filter.append(filterTitle,matches,filterActions);if(!group.unmatchedOnly)box.append(filter);
      const actions=document.createElement('div'); actions.className='group-actions';
      for(const [label,action] of [['上移',()=>{if(index){[groups[index-1],groups[index]]=[groups[index],groups[index-1]];}}],
        ['下移',()=>{if(index<groups.length-1){[groups[index+1],groups[index]]=[groups[index],groups[index+1]];}}],
        ['删除',()=>groups.splice(index,1)]]) {
        const button=document.createElement('button'); button.type='button'; button.className='secondary'; button.textContent=label;
        button.setAttribute('aria-label',`${label}目录 ${index+1}`);
        if(label==='删除')button.classList.add('remove-rule');
        if((label==='上移'&&index===0)||(label==='下移'&&index===groups.length-1))button.disabled=true;
        button.addEventListener('click',()=>{action();renderGroups();updateSave();}); actions.append(button);
      }
      box.append(actions); byId('outline-groups').append(box);
    });
    renderNewProjectTarget();
  }
  byId('add-group').addEventListener('click',()=>{groups.push({headingFields:[],path:[groups[0]?.path[0] || '本周工作','新项目','新事项'],sources:new Set(),match:{}});renderGroups();updateSave();});
  function renderNewProjectTarget() {
    const select=byId('new-project-target');
    select.replaceChildren(new Option('不接纳，未匹配时提示补充规则','none'),new Option('单独归入“待归类”目录','separate'));
    groups.forEach((group,index)=>{
      if(!group.unmatchedOnly&&!group.headingFields.some(Boolean))select.append(new Option(group.path.filter(Boolean).join(' / ') || `目录 ${index+1}`,String(index)));
    });
    const target=groups.findIndex(group=>group.acceptUnmatched);
    select.value=groups.some(group=>group.unmatchedOnly)?'separate':target>=0?String(target):'none';
  }
  let detachedFallback=null;
  byId('new-project-target').addEventListener('change',event=>{
    const value=event.target.value;
    const target=value==='none'||value==='separate'?null:groups[Number(value)];
    const existing=groups.find(group=>group.unmatchedOnly);
    if(existing)detachedFallback=existing;
    for(let index=groups.length-1;index>=0;index--){
      groups[index].acceptUnmatched=false;
      if(groups[index].unmatchedOnly)groups.splice(index,1);
    }
    if(value==='separate'){
      const assigned=assignments(), project=Object.keys(assigned).find(name=>assigned[name]==='project');
      if(!project){alert('请先将一列映射为项目 / 分组。');renderGroups();updateSave();return;}
      const sources=new Set(groups.flatMap(group=>[...group.sources]));
      groups.push(detachedFallback || {headingFields:[groups[0]?.headingFields[0] || '','',project],
        path:[groups[0]?.path[0] || '报告内容','待归类',project],sources,match:{},unmatchedOnly:true});
    }else if(target)target.acceptUnmatched=true;
    renderGroups();updateSave();
  });
  let statusChecks=[];
  function renderStatuses() {
    const previous = new Map(statusChecks.map(item => [item.value, item.check.checked]));
    byId('status-options').replaceChildren(); statusChecks=[];
    const row=rows.find(row=>row.select.value==='status');
    byId('status-policy').hidden=!row;
    if(!row) return;
    const known=base.input.status_policy?.known || [], included=base.input.status_policy?.include || [];
    for(const value of row.column.values || Object.keys(row.column.statuses || {})) {
      const check=input(`保留状态 ${value}`,previous.has(value)?previous.get(value):!known.includes(value)||included.includes(value),'checkbox');
      const label=document.createElement('label'); label.className='profile-check'; label.append(check,document.createTextNode(value));
      byId('status-options').append(label); statusChecks.push({value,check});
    }
  }
  renderGroups(); renderStatuses();
  for(const [role,fallback] of Object.entries(defaults)) {
    const values={...fallback,...(base.document.styles?.[role] || {})};
    const tr=document.createElement('tr'), th=document.createElement('th'); th.scope='row';
    th.textContent=role==='title'?'报告标题':role==='body_1'?'正文':headingLabels[Number(role.slice(-1))-1]; tr.append(th);
    const controls={};
    for(const key of ['font','size_pt','bold','line_spacing']) {
      let node;
      if(key==='font') {
        node=document.createElement('select'); node.setAttribute('aria-label',`${th.textContent} 字体`);
        const fonts=[['Microsoft YaHei','微软雅黑'],['SimSun','宋体'],['SimHei','黑体'],['FangSong','仿宋'],['KaiTi','楷体'],['Arial','Arial'],['Times New Roman','Times New Roman']];
        if(!fonts.some(([value])=>value===values.font)) fonts.push([values.font,values.font]);
        for(const [value,label] of fonts) node.append(new Option(label,value));
        node.value=values.font;
      } else if(key==='bold') {
        node=document.createElement('select'); node.setAttribute('aria-label',`${th.textContent} 加粗`);
        for(const [v,t] of [['true','加粗'],['false','不加粗'],['null','自行决定']]) node.append(new Option(t,v));
        node.value=String(values.bold);
      } else {
        node=input(`${th.textContent} ${key==='size_pt'?'字号':'行距'}`,values[key],'number'); node.required=true;
        if(key==='size_pt'){node.min=5;node.max=72;node.step=.5;}
        if(key==='line_spacing'){node.min=1;node.max=3;node.step='any';}
      }
      const td=document.createElement('td');td.append(node);tr.append(td);controls[key]=node;
    }
    styleRows.push({role,controls});byId('style-rows').append(tr);
  }
  ['heading_1','heading_2','heading_3','body_1','body_2','body_3'].forEach(role=>{
    const choices=numberingChoices[role];
    const select=document.createElement('select');
    const examples={h1:9,h2:1,h3:1,n:1,circle:'①'};
    for(const pattern of choices) select.append(new Option(pattern.replace(/\{(\w+)\}/g,(_,key)=>examples[key]),pattern));
    select.value=base.document.numbering?.[role] || choices[0];
    const label=role.startsWith('heading')?headingLabels[Number(role.slice(-1))-1]:`${['一级','二级','三级'][Number(role.slice(-1))-1]}正文`;
    byId('numbering-controls').append(field(label,select));numberingRows.push({role,select});
  });
  const today=new Date(); byId('trial-date').value=`${today.getFullYear()}-${String(today.getMonth()+1).padStart(2,'0')}-${String(today.getDate()).padStart(2,'0')}`;
  function buildRule() {
    const rule=structuredClone(base), assigned=assignments(); rule.id='adapted';rule.name=byId('profile-name').value.trim();
    const fields=Object.values(assigned);
    if(new Set(fields).size!==fields.length) throw Error('每个语义用途只能选择一列；其他正文列请选择其他内容，再勾选写入正文。');
    rule.input.column_aliases=Object.fromEntries(Object.entries(assigned).map(([column,key])=>[key,[column]]));
    if(groups.filter(group=>group.unmatchedOnly||group.acceptUnmatched).length>1)throw Error('只能选择一个接纳新增项目的目标目录。');
    rule.sections=groups.map((group,index)=>{
      const path=group.path.map((value,level)=>(group.headingFields[level] || value).trim());while(path.length&&!path.at(-1))path.pop();
      const headingFields=path.map((_,level)=>{
        const name=group.headingFields[level];
        if(name&&!assigned[name])throw Error(`目录 ${index+1} 的标题来源字段已停用，请重新选择。`);
        return name?assigned[name]:'';
      });
      if(group.acceptUnmatched&&headingFields.some(Boolean))throw Error('接纳新增项目的已有目录须使用固定标题，请重新选择目标目录。');
      if(!path.length||path.some(value=>!value))throw Error(`目录 ${index+1} 的标题层级不完整。`);
      const selectedSources=[...group.sources].filter(name=>assigned[name]);
      selectedSources.sort((a,b)=>(assigned[b]==='content')-(assigned[a]==='content'));
      const sourceFields=selectedSources.map(name=>assigned[name]).filter(Boolean);
      if(!fields.includes('content'))throw Error('请将一列映射为主要正文。');
      if(!sourceFields.length)throw Error(`请选择目录 ${index+1} 要整合的内容。`);
      const match={};for(const [key,values] of Object.entries(group.match)){if(fields.includes(key)&&values.length)match[key]=values;}
      return {id:`section_${index+1}`,title:path.at(-1),source_field:sourceFields[0],source_fields:sourceFields,
        heading_path:path,heading_fields:headingFields,match,unmatched_only:Boolean(group.unmatchedOnly),accept_unmatched:Boolean(group.acceptUnmatched),group_by:'section',llm_editable:sourceFields.some(key=>!['project','detail','status','owner'].includes(key))};
    });
    if(statusChecks.length)rule.input.status_policy={known:statusChecks.map(item=>item.value),include:statusChecks.filter(item=>item.check.checked).map(item=>item.value),unknown:'keep'};
    else delete rule.input.status_policy;
    rule.document.report_title=byId('report-title').value.trim();rule.document.heading_start_default=Number(byId('heading-start').value);
    rule.document.styles={...(rule.document.styles||{})};
    for(const {role,controls} of styleRows){
      const values=Object.fromEntries(Object.entries(controls).map(([key,node])=>[key,key==='font'?node.value.trim():key==='bold'?JSON.parse(node.value):Number(node.value)]));
      const targets=role==='body_1'?['body_1','body_2','body_3']:[role];
      for(const target of targets)rule.document.styles[target]={...(rule.document.styles[target]||{}),...values};
    }
    rule.document.numbering=Object.fromEntries(numberingRows.map(({role,select})=>[role,select.value]));
    return rule;
  }
  let tested=null;
  function updateSave(){let same=false;try{same=tested===JSON.stringify(buildRule());}catch(_){}byId('save-profile').disabled=!same||!byId('confirm-profile').checked;}
  form.addEventListener('input',updateSave);form.addEventListener('change',updateSave);
  async function post(path,payload){
    const response=await fetch(`/profiles/drafts/${token}/${path}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const result=await response.json();if(!response.ok)throw Error(result.error||'操作失败，请重试');return result;
  }
  form.addEventListener('submit',async event=>{
    event.preventDefault();const button=form.querySelector('[type=submit]');button.disabled=true;tested=null;updateSave();
    byId('trial-status').textContent='正在验证样例规则与格式…';byId('trial-result').hidden=true;
    try{
      const rule=buildRule(),result=await post('trial',{rule,report_date:byId('trial-date').value});tested=JSON.stringify(rule);
      byId('trial-status').textContent=`校验通过：保留 ${result.retained} 行，组成 ${result.blocks} 个审核区块，排除 ${result.excluded} 行。${result.warnings.join(' ')}`;
      byId('trial-text').textContent=result.paragraphs.map(p=>p.text).join('\n');byId('trial-download').href=result.download_url;byId('trial-result').hidden=false;
    }catch(error){byId('trial-status').textContent=error.message;}finally{button.disabled=false;updateSave();}
  });
  byId('save-profile').addEventListener('click',async()=>{
    byId('save-profile').disabled=true;byId('save-status').textContent='正在保存…';
    try{
      const result=await post('save',{rule:buildRule(),confirmed:byId('confirm-profile').checked});
      byId('save-status').textContent=`方案已保存（v${result.version}）。`;
      const link=document.createElement('a');link.href=result.url;link.textContent='使用此方案';byId('save-status').append(' ',link);tested=null;
    }catch(error){byId('save-status').textContent=error.message;updateSave();}
  });
})();
