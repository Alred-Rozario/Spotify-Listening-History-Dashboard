import streamlit as st
import duckdb
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

# Set page config for aesthetics
st.set_page_config(page_title="Spotify Listening Dashboard", layout="wide", page_icon="🎵")

st.title("🎵 Spotify Listening History Dashboard")
st.markdown("Explore your listening habits, top tracks, and audio features.")

@st.cache_data
def load_data():
    # Directly query the parquet file using DuckDB for now
    con = duckdb.connect()
    # Filter out zero plays to get meaningful listening data
    df = con.query("SELECT * FROM 'master_tracks.parquet'").df()
    return df

try:
    df = load_data()
except Exception as e:
    st.error(f"Error loading data: {e}")
    st.stop()

# 🔍 Search/filter by artist or date range
st.sidebar.header("🔍 Filters")
min_date = df['played_at_local'].min().date() if not df.empty else pd.to_datetime('2026-01-01').date()
max_date = df['played_at_local'].max().date() if not df.empty else pd.to_datetime('2026-12-31').date()

date_range = st.sidebar.date_input("Date Range", [min_date, max_date])

# Filter by Artist
all_artists = df['artist_name'].dropna().unique().tolist()
selected_artists = st.sidebar.multiselect("Filter by Artist(s)", sorted(all_artists))

# Filter out skips
exclude_skips = st.sidebar.checkbox("Exclude Skips (< 30s)", value=True)

# Apply filters
filtered_df = df.copy()

if len(date_range) == 2:
    start_date, end_date = date_range
    filtered_df = filtered_df[(filtered_df['played_at_local'].dt.date >= start_date) & 
                              (filtered_df['played_at_local'].dt.date <= end_date)]

if selected_artists:
    filtered_df = filtered_df[filtered_df['artist_name'].isin(selected_artists)]

unskipped_filtered_df = filtered_df.copy()

if exclude_skips:
    filtered_df = filtered_df[filtered_df['is_skip'] == False]

if filtered_df.empty:
    st.warning("No data available for the selected filters.")
    st.stop()


# Navigation
st.sidebar.markdown("---")
page = st.sidebar.radio("Navigation", ["Overview", "Skip Analysis", "Mood & Vibe", "Era Time-Travel"])

if page == "Overview":
    # 📊 Top 10 artists & tracks (bar charts)
    st.markdown("### 🏆 Top 10 Artists & Tracks")
    col1, col2 = st.columns(2)
    
    with col1:
        top_artists = filtered_df.groupby('artist_name')['ms_played'].sum().reset_index()
        top_artists['minutes_played'] = top_artists['ms_played'] / 60000
        top_artists = top_artists.sort_values(by='minutes_played', ascending=False).head(10)
        fig_artists = px.bar(top_artists, x='minutes_played', y='artist_name', orientation='h',
                             title="Top 10 Artists by Minutes Played",
                             labels={'minutes_played': 'Minutes Played', 'artist_name': 'Artist'},
                             color='minutes_played', color_continuous_scale='Purples')
        fig_artists.update_layout(yaxis={'categoryorder':'total ascending'})
        st.plotly_chart(fig_artists, use_container_width=True)
    
    with col2:
        top_tracks = filtered_df.groupby(['track_name', 'artist_name'])['ms_played'].sum().reset_index()
        top_tracks['minutes_played'] = top_tracks['ms_played'] / 60000
        top_tracks['track_artist'] = top_tracks['track_name'] + " - " + top_tracks['artist_name']
        top_tracks = top_tracks.sort_values(by='minutes_played', ascending=False).head(10)
        fig_tracks = px.bar(top_tracks, x='minutes_played', y='track_artist', orientation='h',
                            title="Top 10 Tracks by Minutes Played",
                            labels={'minutes_played': 'Minutes Played', 'track_artist': 'Track'},
                            color='minutes_played', color_continuous_scale='Purples')
        fig_tracks.update_layout(yaxis={'categoryorder':'total ascending'})
        st.plotly_chart(fig_tracks, use_container_width=True)
    
    # 🕐 Listening heatmap by hour + day of week
    st.markdown("### 🕐 Listening Heatmap by Hour & Day of Week")
    heatmap_data = filtered_df.groupby(['day_of_week', 'hour'])['play_id'].count().reset_index()
    heatmap_data.rename(columns={'play_id': 'play_count'}, inplace=True)
    
    # Order days of week
    days_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    heatmap_pivot = heatmap_data.pivot(index='day_of_week', columns='hour', values='play_count').reindex(days_order).fillna(0)
    
    fig_heatmap = px.imshow(heatmap_pivot,
                            labels=dict(x="Hour of Day", y="Day of Week", color="Play Count"),
                            x=heatmap_pivot.columns,
                            y=heatmap_pivot.index,
                            color_continuous_scale='Purples',
                            aspect='auto')
    st.plotly_chart(fig_heatmap, use_container_width=True)
    
    # 📈 Monthly listening trend (line chart)
    st.markdown("### 📈 Monthly Listening Trend")
    trend_data = filtered_df.groupby('year_month')['ms_played'].sum().reset_index()
    trend_data['hours_played'] = trend_data['ms_played'] / (1000 * 60 * 60)
    trend_data = trend_data.sort_values('year_month')
    
    fig_trend = px.line(trend_data, x='year_month', y='hours_played', markers=True,
                        title="Total Hours Played per Month",
                        labels={'year_month': 'Month', 'hours_played': 'Hours Played'})
    fig_trend.update_traces(line_color='#967BB6', marker=dict(size=8))
    st.plotly_chart(fig_trend, use_container_width=True)

elif page == "Skip Analysis":
    # 1. Skip Rate Hall of Fame/Shame
    st.markdown("### ⏭️ Skip Rate Hall of Fame & Shame")
    st.markdown("Artists you skip the most vs. never skip.")
    
    skip_stats = unskipped_filtered_df.groupby('artist_name').agg(
        total_plays=('play_id', 'count'),
        skips=('is_skip', 'sum')
    ).reset_index()
    
    skip_stats = skip_stats[skip_stats['total_plays'] >= 5]
    skip_stats['skip_rate'] = (skip_stats['skips'] / skip_stats['total_plays']) * 100
    
    if not skip_stats.empty:
        col_skip1, col_skip2 = st.columns(2)
        
        with col_skip1:
            most_skipped = skip_stats.sort_values(by='skip_rate', ascending=False).head(5)
            fig_shame = px.bar(most_skipped, x='skip_rate', y='artist_name', orientation='h',
                               title="Hall of Shame (Highest Skip Rate)",
                               labels={'skip_rate': 'Skip Rate (%)', 'artist_name': 'Artist'},
                               color='skip_rate', color_continuous_scale='Reds')
            fig_shame.update_layout(yaxis={'categoryorder':'total ascending'})
            st.plotly_chart(fig_shame, use_container_width=True)
            
        with col_skip2:
            least_skipped = skip_stats.sort_values(by='skip_rate', ascending=True).head(5)
            fig_fame = px.bar(least_skipped, x='skip_rate', y='artist_name', orientation='h',
                              title="Hall of Fame (Lowest Skip Rate)",
                              labels={'skip_rate': 'Skip Rate (%)', 'artist_name': 'Artist'},
                              color='skip_rate', color_continuous_scale='Purples')
            fig_fame.update_layout(yaxis={'categoryorder':'total descending'})
            st.plotly_chart(fig_fame, use_container_width=True)
    else:
        st.info("Not enough data to calculate skip rates. Try adjusting filters or listening more!")

elif page == "Mood & Vibe":
    # 🎵 Audio features radar chart
    st.markdown("### 🎵 Average Audio Features")
    st.info("Note: Audio features might be missing for some tracks due to API limitations.")
    
    features_df = filtered_df[filtered_df['has_audio_features'] == True]
    
    if features_df.empty:
        st.warning("No audio features available for the selected filters.")
    else:
        features = ['danceability', 'energy', 'valence', 'acousticness', 'instrumentalness', 'liveness', 'speechiness']
        avg_features = features_df[features].mean().reset_index()
        avg_features.columns = ['feature', 'value']
    
        fig_radar = go.Figure(data=go.Scatterpolar(
            r=avg_features['value'],
            theta=avg_features['feature'],
            fill='toself',
            marker=dict(color='#967BB6'),
            line=dict(color='#967BB6')
        ))
        
        fig_radar.update_layout(
            polar=dict(
                radialaxis=dict(
                    visible=True,
                    range=[0, 1]
                )),
            showlegend=False
        )
        st.plotly_chart(fig_radar, use_container_width=True)
    
    # 2. Mood & Vibe Analysis (Time of Day)
    st.markdown("### 🎭 Mood & Vibe by Time of Day")
    st.markdown("How does your music vibe change from morning to night?")
    if not features_df.empty:
        vibe_hourly = features_df.groupby('hour')[['energy', 'danceability', 'valence', 'acousticness']].mean().reset_index()
        
        fig_vibe = go.Figure()
        fig_vibe.add_trace(go.Scatter(x=vibe_hourly['hour'], y=vibe_hourly['energy'], mode='lines+markers', name='Energy', line=dict(color='#FF7F50')))
        fig_vibe.add_trace(go.Scatter(x=vibe_hourly['hour'], y=vibe_hourly['danceability'], mode='lines+markers', name='Danceability', line=dict(color='#9370DB')))
        fig_vibe.add_trace(go.Scatter(x=vibe_hourly['hour'], y=vibe_hourly['valence'], mode='lines+markers', name='Valence (Happiness)', line=dict(color='#FFD700')))
        fig_vibe.add_trace(go.Scatter(x=vibe_hourly['hour'], y=vibe_hourly['acousticness'], mode='lines+markers', name='Acousticness', line=dict(color='#2E8B57')))
        
        fig_vibe.update_layout(title="Average Audio Features Throughout the Day",
                               xaxis_title="Hour of Day (0-23)",
                               yaxis_title="Average Value",
                               xaxis=dict(tickmode='linear', tick0=0, dtick=1))
        st.plotly_chart(fig_vibe, use_container_width=True)
    else:
        st.info("No audio features available to analyze mood.")

elif page == "Era Time-Travel":
    # 5. Release Year / Era Time-Travel
    st.markdown("### ⏳ Era Time-Travel")
    st.markdown("What decades/years is your music from?")
    
    def extract_year(date_str):
        try:
            return int(str(date_str)[:4])
        except:
            return None
    
    if 'release_date' in filtered_df.columns:
        era_df = filtered_df.copy()
        era_df['release_year'] = era_df['release_date'].apply(extract_year)
        era_df = era_df.dropna(subset=['release_year'])
        
        if not era_df.empty:
            fig_era = px.histogram(era_df, x='release_year', nbins=30,
                                   title="Distribution of Listening by Release Year",
                                   labels={'release_year': 'Release Year', 'count': 'Number of Plays'},
                                   color_discrete_sequence=['#967BB6'])
            st.plotly_chart(fig_era, use_container_width=True)
        else:
            st.info("No valid release year data found.")
    else:
        st.info("Release date data is missing from the dataset.")
