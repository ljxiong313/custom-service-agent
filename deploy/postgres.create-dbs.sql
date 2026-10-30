-- 首次启动时给每个店建一个独立数据库（多店 = 多套实例，隔离的是数据，见 docs/运维手册.md §1）。
--
-- 只在 pgdata 卷为空时执行一次（官方镜像的 /docker-entrypoint-initdb.d 语义）。
-- 已经跑过一次的库不会再执行这里：加店时要手工建库，别等它——
--   docker compose -f deploy/docker-compose.shop.yml exec postgres \
--     psql -U cs_agent -c 'CREATE DATABASE cs_shop_c'
-- 建完直接 up 那个新店即可：migrate_mode: alembic 会在新库里把表建出来（全新空库不需要 stamp，
-- stamp 只用于"以前用 auto 模式跑出来的旧库"，口径见 docs/运维手册.md §7）。
--
-- 一店一库而不是同库多 schema：代码里的表名不带 schema 前缀，共用 schema 要靠 search_path
-- 做隔离，属于没验证过的路径；分库则天然干净，备份/恢复/权限也按店走。

CREATE DATABASE cs_shop_a;
CREATE DATABASE cs_shop_b;

-- 口令与库名都在 deploy/env/shop-*.env 里，这个文件是可以进 git 的（没有密钥）。
-- 加店就再加一行。角色沿用镜像初始化出来的 POSTGRES_USER=cs_agent（超级用户， owns 这些库）。
